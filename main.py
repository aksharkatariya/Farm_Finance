import os
import json
import logging
import sqlite3
from datetime import datetime
from typing import Final

# External libraries
from google import genai # NEW Google GenAI SDK
from telegram import Update
from telegram.ext import (
    Application, 
    CommandHandler, 
    MessageHandler, 
    filters, 
    ContextTypes, 
    ConversationHandler
)

# PDF Libraries
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet

# Load environment variables (Ensure these exact keys are set in Railway Variables)
API_TOKEN: Final = os.getenv('TELEGRAM_BOT_TOKEN')
GEN_API_KEY: Final = os.getenv('GEMINI_API_KEY')

# Conversation States
GET_NAME, GET_ADDRESS, GET_EXPENSES, GET_EARNINGS = range(4)

# Logging setup
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# -------------------------------
# Database Logic
# -------------------------------
def init_db():
    conn = sqlite3.connect('finance_bot.db')
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS users 
                      (user_id INTEGER PRIMARY KEY, name TEXT, address TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS transactions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER,
                        type TEXT,
                        amount REAL,
                        category TEXT,
                        date TEXT)''')
    conn.commit()
    conn.close()

def save_transaction(user_id, t_type, amount, category, date_str):
    conn = sqlite3.connect('finance_bot.db')
    cursor = conn.cursor()
    # Default to today if date is missing
    final_date = date_str if date_str else datetime.now().strftime("%Y-%m-%d")
    cursor.execute("INSERT INTO transactions (user_id, type, amount, category, date) VALUES (?, ?, ?, ?, ?)",
                   (user_id, t_type, amount, category, final_date))
    conn.commit()
    conn.close()

# -------------------------------
# PDF Generation Logic
# -------------------------------
def generate_pdf(user_name, address, transactions, filename):
    doc = SimpleDocTemplate(filename, pagesize=letter)
    elements = []
    styles = getSampleStyleSheet()

    # Title & Header
    elements.append(Paragraph(f"Financial Report: {datetime.now().strftime('%B %Y')}", styles['Title']))
    elements.append(Paragraph(f"<b>Name:</b> {user_name}", styles['Normal']))
    elements.append(Paragraph(f"<b>Address:</b> {address}", styles['Normal']))
    elements.append(Spacer(1, 12))

    # Table Header
    data = [["Type", "Category", "Amount", "Date"]]
    total_earnings = 0
    total_expenses = 0

    for t_type, cat, amt, date in transactions:
        data.append([t_type.capitalize(), cat, f"${amt:,.2f}", date])
        if t_type == 'earning': total_earnings += amt
        else: total_expenses += amt

    # Table Styling
    t = Table(data, colWidths=[80, 200, 100, 100])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.teal),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.whitesmoke, colors.lightgrey])
    ]))
    elements.append(t)

    # Summary
    elements.append(Spacer(1, 24))
    elements.append(Paragraph(f"<b>Total Earnings:</b> ${total_earnings:,.2f}", styles['Normal']))
    elements.append(Paragraph(f"<b>Total Expenses:</b> ${total_expenses:,.2f}", styles['Normal']))
    elements.append(Paragraph(f"<b>Net Balance:</b> ${total_earnings - total_expenses:,.2f}", styles['Normal']))

    doc.build(elements)

# -------------------------------
# AI Parsing Logic
# -------------------------------
def ai_parse_finance(text: str, mode: str):
    current_month_year = datetime.now().strftime("%Y-%m")
    current_full_date = datetime.now().strftime("%Y-%m-%d")
    current_display = datetime.now().strftime('%B %Y')
    
    prompt = f"""
    You are a professional farm accountant. Analyze this text: "{text}"
    Mode: This text describes {mode.upper()}S for the current period ({current_display}).

    ### LOGIC RULES:
    1. **Percentage Calculation (The "Out Of" Rule):** - If a total is given (e.g., "Out of 5000"), apply all following percentages to that total.
       - "20% on seeds" = 0.20 * 5000 = 1000.
       - "The rest" or "balance" = Total minus all other calculated parts.

    2. **Accrual vs Cash Rule (The "4000 Sales" Rule):**
       - If the user says they earned/spent a total but will receive/pay a portion later, record the **TOTAL** amount as the transaction value for this month. 
       - Example: "Earned 4000, 20% next month" -> amount: 4000.0, category: "Crop Sales".

    3. **Date Intelligence:**
       - "On the 5th" -> "{current_month_year}-05".
       - "Yesterday" -> Calculate based on today's date ({current_full_date}).
       - "Next month" -> If it's part of a payment split, ignore the date change and record it for the current month unless explicitly a separate transaction.
       - If no date is mentioned, use "{current_full_date}".

    4. **Categorization:**
       - Create concise, professional categories (e.g., "Seeds", "Labor", "Pesticides", "Harvest Revenue").
       - If the user mentions multiple items, split them into separate objects in the array.

    ### OUTPUT FORMAT:
    - Return ONLY a JSON array of objects. No conversational text.
    - Example: [{{ "amount": 1200.50, "category": "Labor", "date": "2026-03-13" }}]

    ### INPUT TO PROCESS:
    "{text}"
    """
    try:
        # NEW Google GenAI implementation
        client = genai.Client(api_key=GEN_API_KEY)
        response = client.models.generate_content(
            model="gemini-2.5-flash", 
            contents=prompt,
            config=genai.types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.0
            )
        )
        return json.loads(response.text)
    except Exception as e:
        logger.error(f"AI Parsing Error: {e}")
        return []

# -------------------------------
# Handlers
# -------------------------------
async def start_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hi! Let's get your profile set up. What is your **name**?")
    return GET_NAME

async def handle_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['name'] = update.message.text
    await update.message.reply_text(f"Got it, {update.message.text}. Now, what is your **address**?")
    return GET_ADDRESS

async def handle_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    name = context.user_data['name']
    address = update.message.text
    
    conn = sqlite3.connect('finance_bot.db')
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO users (user_id, name, address) VALUES (?, ?, ?)", (user_id, name, address))
    conn.commit()
    conn.close()

    await update.message.reply_text("Profile saved! Please list your **expenses** for this month in plain English:")
    return GET_EXPENSES

async def handle_expenses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_chat_action("typing")
    data = ai_parse_finance(update.message.text, "expense")
    
    if not data:
        await update.message.reply_text("I couldn't find any expenses. Please try again (e.g., 'Coffee 5, Lunch 10').")
        return GET_EXPENSES

    for item in data:
        save_transaction(update.message.from_user.id, 'expense', item['amount'], item['category'], item.get('date'))
    
    await update.message.reply_text("Expenses saved. Now, please list your **earnings** for this month:")
    return GET_EARNINGS

async def handle_earnings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_chat_action("typing")
    data = ai_parse_finance(update.message.text, "earning")
    
    for item in data:
        save_transaction(update.message.from_user.id, 'earning', item['amount'], item['category'], item.get('date'))
    
    await update.message.reply_text("All set! Type /report to generate your monthly PDF.")
    return ConversationHandler.END

async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    conn = sqlite3.connect('finance_bot.db')
    cursor = conn.cursor()
    
    cursor.execute("SELECT name, address FROM users WHERE user_id = ?", (user_id,))
    user = cursor.fetchone()
    
    if not user:
        await update.message.reply_text("Please say 'hi' to set up your profile first!")
        return

    cursor.execute("SELECT type, category, amount, date FROM transactions WHERE user_id = ? ORDER BY date DESC", (user_id,))
    txs = cursor.fetchall()

    if not txs:
        await update.message.reply_text("No financial data found.")
        return

    file_path = f"report_{user_id}.pdf"
    generate_pdf(user[0], user[1], txs, file_path)

    with open(file_path, 'rb') as pdf:
        await update.message.reply_document(document=pdf, filename="Monthly_Report.pdf", caption="Here is your PDF report! 📄")
    
    os.remove(file_path)
    conn.close()

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Conversation cancelled.")
    return ConversationHandler.END

# -------------------------------
# Main
# -------------------------------
def main():
    if not API_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN is not set.")
        return
        
    init_db()
    app = Application.builder().token(API_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('^(hi|Hi|Hello|hello)$'), start_conversation)],
        states={
            GET_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_name)],
            GET_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_address)],
            GET_EXPENSES: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_expenses)],
            GET_EARNINGS: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_earnings)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler('report', report_command))

    print("Bot is live... Say 'Hi' to start.")
    app.run_polling()

if __name__ == "__main__":
    main()
