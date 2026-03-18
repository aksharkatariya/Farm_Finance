import os
import json
import logging
import sqlite3
from datetime import datetime
from typing import Final

# External libraries
from google import genai # Google GenAI SDK
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

# Load environment variables
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
# AI Logic
# -------------------------------
def ai_parse_finance(text: str, mode: str):
    """Parses natural language into structured JSON transaction data."""
    current_month_year = datetime.now().strftime("%Y-%m")
    current_full_date = datetime.now().strftime("%Y-%m-%d")
    current_display = datetime.now().strftime('%B %Y')
    
    prompt = f"""
    You are a professional accountant. Analyze this text: "{text}"
    Mode: This text describes {mode.upper()}S.

    Rules:
    1. Handle percentages ("20% of 1000").
    2. Handle accrual/cash splits (record the total transaction amount).
    3. Infer dates. No date = "{current_full_date}".
    4. Provide clean categories.
    
    Return ONLY a JSON array of objects: [{{ "amount": 1200.50, "category": "Labor", "date": "2026-03-13" }}]
    """
    try:
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

def ai_financial_advisor(question: str, context_data: dict) -> str:
    """Answers user questions based on their financial history and current balances."""
    prompt = f"""
    You are a professional, friendly financial advisor.

    Here is the user's current financial snapshot:
    - Total Earnings: ${context_data['total_earnings']:,.2f}
    - Total Expenses: ${context_data['total_expenses']:,.2f}
    - Current Net Balance: ${context_data['net_balance']:,.2f}

    Here are their recent transactions for context:
    {context_data['recent_transactions']}

    User's Question: "{question}"

    Answer the user's question directly. 
    - If they ask about affordability (e.g., "Can I afford X?"), analyze their net balance.
    - Be concise, practical, and helpful. Do not use complex markdown, just standard text.
    """
    try:
        client = genai.Client(api_key=GEN_API_KEY)
        response = client.models.generate_content(
            model="gemini-2.5-flash", 
            contents=prompt
        )
        return response.text
    except Exception as e:
        logger.error(f"AI Advisor Error: {e}")
        return "I'm having trouble analyzing your finances right now. Please try again later."

# -------------------------------
# Handlers (Setup Workflow)
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
    
    for item in data:
        save_transaction(update.message.from_user.id, 'expense', item['amount'], item['category'], item.get('date'))
    
    await update.message.reply_text("Expenses saved. Now, please list your **earnings** for this month:")
    return GET_EARNINGS

async def handle_earnings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_chat_action("typing")
    data = ai_parse_finance(update.message.text, "earning")
    
    for item in data:
        save_transaction(update.message.from_user.id, 'earning', item['amount'], item['category'], item.get('date'))
    
    await update.message.reply_text("All set! Type /report to generate your monthly PDF.\nYou can also use /expense, /earning, or /ask anytime!")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Conversation cancelled.")
    return ConversationHandler.END

# -------------------------------
# Handlers (Standalone Commands)
# -------------------------------
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

async def add_expense_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Please provide details. Example: /expense paid $50 for tractor fuel")
        return
    
    text = " ".join(context.args)
    await update.message.reply_chat_action("typing")
    
    data = ai_parse_finance(text, "expense")
    if not data:
        await update.message.reply_text("I couldn't understand that. Try being more specific.")
        return

    for item in data:
        save_transaction(update.message.from_user.id, 'expense', item['amount'], item['category'], item.get('date'))
    
    await update.message.reply_text(f"✅ Added {len(data)} expense(s) successfully!")

async def add_earning_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Please provide details. Example: /earning sold crops for 4000")
        return
    
    text = " ".join(context.args)
    await update.message.reply_chat_action("typing")
    
    data = ai_parse_finance(text, "earning")
    if not data:
        await update.message.reply_text("I couldn't understand that. Try being more specific.")
        return

    for item in data:
        save_transaction(update.message.from_user.id, 'earning', item['amount'], item['category'], item.get('date'))
    
    await update.message.reply_text(f"✅ Added {len(data)} earning(s) successfully!")

async def ask_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Ask me a financial question! Example: /ask Can I afford a $300 tool?")
        return

    question = " ".join(context.args)
    await update.message.reply_chat_action("typing")
    user_id = update.message.from_user.id

    # Fetch data from DB
    conn = sqlite3.connect('finance_bot.db')
    cursor = conn.cursor()
    cursor.execute("SELECT type, category, amount, date FROM transactions WHERE user_id = ? ORDER BY date DESC", (user_id,))
    txs = cursor.fetchall()
    conn.close()

    # Calculate balances
    total_earnings = sum(t[2] for t in txs if t[0] == 'earning')
    total_expenses = sum(t[2] for t in txs if t[0] == 'expense')
    net_balance = total_earnings - total_expenses

    # Format the last 15 transactions for context
    recent_txs = [f"{t[3]}: {t[0].upper()} - {t[1]} (${t[2]})" for t in txs[:15]]
    tx_string = "\n".join(recent_txs) if recent_txs else "No recent transactions found."

    context_data = {
        "total_earnings": total_earnings,
        "total_expenses": total_expenses,
        "net_balance": net_balance,
        "recent_transactions": tx_string
    }

    # Ask AI
    answer = ai_financial_advisor(question, context_data)
    await update.message.reply_text(answer)

# -------------------------------
# Main
# -------------------------------
def main():
    if not API_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN is not set.")
        return
        
    init_db()
    app = Application.builder().token(API_TOKEN).build()

    # Setup Workflow
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

    # Register Handlers
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler('report', report_command))
    app.add_handler(CommandHandler('expense', add_expense_command))
    app.add_handler(CommandHandler('earning', add_earning_command))
    app.add_handler(CommandHandler('ask', ask_command))

    print("Bot is live... Ready for commands.")
    app.run_polling()

if __name__ == "__main__":
    main()