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
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

# Load environment variables
API_TOKEN: Final = os.getenv('TELEGRAM_BOT_TOKEN')
GEN_API_KEY: Final = os.getenv('GEMINI_API_KEY')

# Conversation States
GET_NAME, GET_ADDRESS, GET_GST, GET_FARM_SIZE, GET_FARM_METRICS, GET_EXPENSES, GET_EARNINGS = range(7)

# Logging setup
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# -------------------------------
# Database Logic
# -------------------------------
def init_db():
    conn = sqlite3.connect('finance_bot.db')
    cursor = conn.cursor()
    
    # Create or update Users table
    cursor.execute('''CREATE TABLE IF NOT EXISTS users 
                      (user_id INTEGER PRIMARY KEY, name TEXT, address TEXT)''')
    
    # Safely add new columns if they don't exist (upgrading existing databases)
    try: cursor.execute("ALTER TABLE users ADD COLUMN gst_number TEXT")
    except sqlite3.OperationalError: pass
    
    try: cursor.execute("ALTER TABLE users ADD COLUMN farm_size REAL")
    except sqlite3.OperationalError: pass

    # Create Farm Metrics table
    cursor.execute('''CREATE TABLE IF NOT EXISTS farm_metrics (
                        user_id INTEGER PRIMARY KEY,
                        cultivated_acres REAL,
                        crop_details TEXT)''')

    # Create Transactions table
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
# AI Logic
# -------------------------------
def ai_parse_farm_metrics(text: str):
    """Parses natural language about farm sizes and crops into structured JSON."""
    prompt = f"""
    Analyze this text describing a farm's current cultivation: "{text}"
    Extract:
    1. The total cultivated area in acres (as a number). If given in hectares, convert to acres (1 ha = 2.47 acres).
    2. A clean, brief summary string of the crops and their respective areas.
    
    Return ONLY a JSON object exactly like this: 
    {{ "cultivated_acres": 50.5, "crop_details": "30 acres Wheat, 20.5 acres Corn" }}
    """
    try:
        client = genai.Client(api_key=GEN_API_KEY)
        response = client.models.generate_content(
            model="gemini-2.5-flash", 
            contents=prompt,
            config=genai.types.GenerateContentConfig(response_mime_type="application/json", temperature=0.0)
        )
        return json.loads(response.text)
    except Exception as e:
        logger.error(f"AI Parse Farm Metrics Error: {e}")
        return {"cultivated_acres": 0, "crop_details": "Unknown"}

def ai_parse_finance(text: str, mode: str):
    """Parses natural language into structured JSON transaction data."""
    current_full_date = datetime.now().strftime("%Y-%m-%d")
    
    prompt = f"""
    You are a professional accountant. Analyze this text: "{text}"
    Mode: This text describes {mode.upper()}S.

    Rules:
    1. Handle percentages ("20% of 1000").
    2. Handle accrual/cash splits (record the total transaction amount).
    3. Infer dates. No date = "{current_full_date}".
    4. Provide clean categories.
    5. CRITICAL: If an expense relates to loans, debt, or interest payments, categorize it strictly as "Debt Service".
    
    Return ONLY a JSON array of objects: [{{ "amount": 1200.50, "category": "Labor", "date": "2026-03-13" }}]
    """
    try:
        client = genai.Client(api_key=GEN_API_KEY)
        response = client.models.generate_content(
            model="gemini-2.5-flash", 
            contents=prompt,
            config=genai.types.GenerateContentConfig(response_mime_type="application/json", temperature=0.0)
        )
        return json.loads(response.text)
    except Exception as e:
        logger.error(f"AI Parsing Error: {e}")
        return []

def ai_financial_advisor(question: str, context_data: dict) -> str:
    """Answers user questions based on their comprehensive farm financial history."""
    prompt = f"""
    You are a professional, highly capable agricultural financial advisor.

    Farm Financial Snapshot:
    - Farm Size: {context_data['farm_size']} acres
    - Cultivated Area: {context_data['cultivated_acres']} acres
    - Crops Grown: {context_data['crop_details']}
    
    Financial Metrics:
    - Total Earnings: ${context_data['total_earnings']:,.2f}
    - Total Expenses: ${context_data['total_expenses']:,.2f}
    - Net Income: ${context_data['net_income']:,.2f}
    - Revenue Yield per Acre: ${context_data['yield_per_acre']:,.2f}
    - Cost per Acre: ${context_data['cost_per_acre']:,.2f}
    - Debt Service Coverage Ratio (DSCR): {context_data['dscr']}
    - Debt to Income Ratio (DTI): {context_data['dti']}

    Recent Transactions Context:
    {context_data['recent_transactions']}

    User's Question: "{question}"

    Answer directly and professionally. Use the calculated metrics (like DSCR or Cost per Acre) to back up your advice if relevant. Be concise, practical, and helpful. Do not use complex markdown formats.
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
# Core Calculators & PDF Generation
# -------------------------------
def calculate_metrics(transactions, cultivated_acres):
    """Calculates core farm financial metrics from raw data."""
    total_earnings = sum(t[2] for t in transactions if t[0] == 'earning')
    total_expenses = sum(t[2] for t in transactions if t[0] == 'expense')
    debt_service = sum(t[2] for t in transactions if t[0] == 'expense' and 'debt' in t[1].lower() or 'interest' in t[1].lower() or 'loan' in t[1].lower())
    
    net_income = total_earnings - total_expenses
    noi = net_income + debt_service # Net Operating Income
    
    yield_per_acre = total_earnings / cultivated_acres if cultivated_acres > 0 else 0
    cost_per_acre = total_expenses / cultivated_acres if cultivated_acres > 0 else 0
    
    dscr = (noi / debt_service) if debt_service > 0 else "N/A (No Debt)"
    dti = (debt_service / total_earnings) * 100 if total_earnings > 0 else 0

    return {
        "total_earnings": total_earnings,
        "total_expenses": total_expenses,
        "net_income": net_income,
        "cash_flow": net_income, # Simplified for standard tracking
        "debt_service": debt_service,
        "yield_per_acre": yield_per_acre,
        "cost_per_acre": cost_per_acre,
        "dscr": dscr if isinstance(dscr, str) else f"{dscr:.2f}x",
        "dti": dti if isinstance(dti, str) else f"{dti:.2f}%"
    }

def generate_pdf(user_data, metrics_data, transactions, filename):
    doc = SimpleDocTemplate(filename, pagesize=letter)
    elements = []
    styles = getSampleStyleSheet()
    title_style = styles['Title']
    sub_title = ParagraphStyle('SubTitle', parent=styles['Heading2'], spaceAfter=10)

    # 1. Official Header
    elements.append(Paragraph(f"Official Farm Financial Report", title_style))
    elements.append(Paragraph(f"<b>Date:</b> {datetime.now().strftime('%B %d, %Y')}", styles['Normal']))
    elements.append(Spacer(1, 12))

    # 2. Profile Information
    elements.append(Paragraph("Farmer Profile & Farm Data", sub_title))
    profile_text = f"""
    <b>Name:</b> {user_data[1]}<br/>
    <b>Address:</b> {user_data[2]}<br/>
    <b>GST Number:</b> {user_data[3]}<br/>
    <b>Total Farm Size:</b> {user_data[4]} acres<br/>
    <b>Cultivated This Season:</b> {metrics_data[1]} acres<br/>
    <b>Crops Grown:</b> {metrics_data[2]}
    """
    elements.append(Paragraph(profile_text, styles['Normal']))
    elements.append(Spacer(1, 15))

    # 3. Financial Metrics Analysis
    calc = calculate_metrics(transactions, metrics_data[1])
    
    elements.append(Paragraph("Core Financial Metrics", sub_title))
    metrics_text = f"""
    <b>Total Earnings:</b> ${calc['total_earnings']:,.2f}<br/>
    <b>Total Expenses:</b> ${calc['total_expenses']:,.2f}<br/>
    <b>Net Income (Cash Flow):</b> ${calc['net_income']:,.2f}<br/>
    <br/>
    <b>Revenue Yield per Acre:</b> ${calc['yield_per_acre']:,.2f}/acre<br/>
    <b>Cost per Acre:</b> ${calc['cost_per_acre']:,.2f}/acre<br/>
    <b>Debt-to-Income Ratio (DTI):</b> {calc['dti']}<br/>
    <b>Debt Service Coverage Ratio (DSCR):</b> {calc['dscr']}
    """
    elements.append(Paragraph(metrics_text, styles['Normal']))
    elements.append(Spacer(1, 20))

    # 4. Transaction Ledger Table
    elements.append(Paragraph("Transaction Ledger", sub_title))
    table_data = [["Type", "Category", "Amount", "Date"]]
    
    for t_type, cat, amt, date in transactions:
        table_data.append([t_type.capitalize(), cat, f"${amt:,.2f}", date])

    t = Table(table_data, colWidths=[80, 200, 100, 100])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2E7D32')), # Dark green header
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.whitesmoke, colors.lightgrey])
    ]))
    elements.append(t)

    doc.build(elements)

# -------------------------------
# Handlers (Setup Workflow)
# -------------------------------
async def start_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    intro = (
        "Hello! I am your AI Farm Financial Assistant. 🌾\n\n"
        "I'm here to help you track your farm's income, expenses, and generate professional financial reports "
        "including Yield per Acre, DSCR, and more.\n\n"
        "First, we need to create your profile. What is your **Full Name**?"
    )
    await update.message.reply_text(intro)
    return GET_NAME

async def handle_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['name'] = update.message.text
    await update.message.reply_text(f"Nice to meet you, {update.message.text}! What is your **Address**?")
    return GET_ADDRESS

async def handle_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['address'] = update.message.text
    await update.message.reply_text("Got it. What is your **GST Number**? (Type 'None' if you don't have one)")
    return GET_GST

async def handle_gst(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['gst'] = update.message.text
    await update.message.reply_text("Thank you. What is your **Total Farm Size** (in acres)? Just send the number.")
    return GET_FARM_SIZE

async def handle_farm_size(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        farm_size = float(update.message.text.replace(',', '').strip())
        context.user_data['farm_size'] = farm_size
        
        # Save user to DB
        user_id = update.message.from_user.id
        conn = sqlite3.connect('finance_bot.db')
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO users (user_id, name, address, gst_number, farm_size) 
            VALUES (?, ?, ?, ?, ?)""", 
            (user_id, context.user_data['name'], context.user_data['address'], context.user_data['gst'], farm_size)
        )
        conn.commit()
        conn.close()

        await update.message.reply_text(
            "Profile saved! Now let's gather your current farm metrics.\n"
            "How many acres are cultivated this season, and which crops are growing in how much area each?\n"
            "(e.g., 'I am cultivating 80 acres total. 50 acres of wheat and 30 acres of corn.')"
        )
        return GET_FARM_METRICS
    except ValueError:
        await update.message.reply_text("Please enter a valid number for your farm size (e.g., 100).")
        return GET_FARM_SIZE

async def handle_farm_metrics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_chat_action("typing")
    metrics = ai_parse_farm_metrics(update.message.text)
    
    user_id = update.message.from_user.id
    conn = sqlite3.connect('finance_bot.db')
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO farm_metrics (user_id, cultivated_acres, crop_details) 
        VALUES (?, ?, ?)""", 
        (user_id, metrics['cultivated_acres'], metrics['crop_details'])
    )
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"Understood. You are cultivating {metrics['cultivated_acres']} acres ({metrics['crop_details']}).\n\n"
        "Now, please list all **expenses** incurred for this crop in plain English. "
        "Please ensure you include any **interest payments, debts, or loans** you are servicing so I can calculate your Debt-to-Income and DSCR ratios."
    )
    return GET_EXPENSES

async def handle_expenses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_chat_action("typing")
    data = ai_parse_finance(update.message.text, "expense")
    
    for item in data:
        save_transaction(update.message.from_user.id, 'expense', item['amount'], item['category'], item.get('date'))
    
    await update.message.reply_text("Expenses successfully recorded. Finally, please list your expected or actual **earnings** for this crop:")
    return GET_EARNINGS

async def handle_earnings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_chat_action("typing")
    data = ai_parse_finance(update.message.text, "earning")
    
    for item in data:
        save_transaction(update.message.from_user.id, 'earning', item['amount'], item['category'], item.get('date'))
    
    summary_message = (
        "✅ **All set! Your profile and initial ledger are fully configured.**\n\n"
        "Here is what I can do for you now:\n"
        "📄 /report - Generates a professional PDF containing your ledger, Yield per Acre, Cost per Acre, Cash flow, DTI, and DSCR metrics.\n"
        "💸 /expense <text> - Quickly add new expenses (e.g., '/expense $400 for tractor fuel').\n"
        "💰 /earning <text> - Quickly add new earnings.\n"
        "🧠 /ask <question> - Ask me any financial question (e.g., '/ask Can I afford to buy $2000 of new fertilizer?'). I will analyze your data and respond."
    )
    await update.message.reply_text(summary_message, parse_mode='Markdown')
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Conversation cancelled. You can say 'hi' to start over.")
    return ConversationHandler.END

# -------------------------------
# Handlers (Standalone Commands)
# -------------------------------
async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    conn = sqlite3.connect('finance_bot.db')
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    user = cursor.fetchone()
    
    cursor.execute("SELECT * FROM farm_metrics WHERE user_id = ?", (user_id,))
    metrics = cursor.fetchone()

    if not user or not metrics:
        await update.message.reply_text("Please say 'hi' to set up your profile and farm metrics first!")
        conn.close()
        return

    cursor.execute("SELECT type, category, amount, date FROM transactions WHERE user_id = ? ORDER BY date DESC", (user_id,))
    txs = cursor.fetchall()
    conn.close()

    if not txs:
        await update.message.reply_text("No financial data found. Use /expense or /earning to add some.")
        return

    file_path = f"Farm_Report_{user_id}.pdf"
    generate_pdf(user, metrics, txs, file_path)

    with open(file_path, 'rb') as pdf:
        await update.message.reply_document(document=pdf, filename="Official_Farm_Financial_Report.pdf", caption="Here is your detailed farm financial report! 📄")
    
    os.remove(file_path)

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
        await update.message.reply_text("Please provide details. Example: /earning sold wheat crops for 4000")
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
        await update.message.reply_text("Ask me a financial question! Example: /ask What is my current Cost per Acre?")
        return

    question = " ".join(context.args)
    await update.message.reply_chat_action("typing")
    user_id = update.message.from_user.id

    conn = sqlite3.connect('finance_bot.db')
    cursor = conn.cursor()
    cursor.execute("SELECT farm_size FROM users WHERE user_id = ?", (user_id,))
    user_data = cursor.fetchone()
    
    cursor.execute("SELECT cultivated_acres, crop_details FROM farm_metrics WHERE user_id = ?", (user_id,))
    farm_metrics = cursor.fetchone()

    cursor.execute("SELECT type, category, amount, date FROM transactions WHERE user_id = ? ORDER BY date DESC", (user_id,))
    txs = cursor.fetchall()
    conn.close()

    if not user_data or not farm_metrics:
        await update.message.reply_text("Please set up your profile first by saying 'hi'.")
        return

    # Calculate rich context
    metrics = calculate_metrics(txs, farm_metrics[0])
    
    recent_txs = [f"{t[3]}: {t[0].upper()} - {t[1]} (${t[2]})" for t in txs[:15]]
    tx_string = "\n".join(recent_txs) if recent_txs else "No recent transactions found."

    context_data = {
        "farm_size": user_data[0],
        "cultivated_acres": farm_metrics[0],
        "crop_details": farm_metrics[1],
        "total_earnings": metrics['total_earnings'],
        "total_expenses": metrics['total_expenses'],
        "net_income": metrics['net_income'],
        "yield_per_acre": metrics['yield_per_acre'],
        "cost_per_acre": metrics['cost_per_acre'],
        "dscr": metrics['dscr'],
        "dti": metrics['dti'],
        "recent_transactions": tx_string
    }

    # Ask AI
    answer = ai_financial_advisor(question, context_data)
    await update.message.reply_text(answer)

# -------------------------------
# Main
# -------------------------------
def main():
    if not API_TOKEN or not GEN_API_KEY:
        print("ERROR: TELEGRAM_BOT_TOKEN or GEMINI_API_KEY is not set.")
        return
        
    init_db()
    app = Application.builder().token(API_TOKEN).build()

    # Setup Workflow
    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('^(hi|Hi|Hello|hello)$'), start_conversation)],
        states={
            GET_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_name)],
            GET_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_address)],
            GET_GST: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_gst)],
            GET_FARM_SIZE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_farm_size)],
            GET_FARM_METRICS: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_farm_metrics)],
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