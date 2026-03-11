import os
import json
import logging
import sqlite3
from datetime import datetime
from typing import Final

# External libraries
from dotenv import load_dotenv
import google.generativeai as genai
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Load environment variables
load_dotenv()

# Configuration
API_TOKEN: Final = os.getenv('TELEGRAM_BOT_TOKEN')
GEN_API_KEY: Final = os.getenv('GEMINI_API_KEY')
BOT_HANDLE: Final = os.getenv('BOT_HANDLE')

if not API_TOKEN or not GEN_API_KEY:
    raise ValueError("Missing API Keys! Ensure TELEGRAM_BOT_TOKEN and GEMINI_API_KEY are in your .env file.")

# Logging setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configure Gemini
genai.configure(api_key=GEN_API_KEY)

# -------------------------------
# Database Logic
# -------------------------------
def init_db():
    """Initializes the SQLite database and creates the table if it doesn't exist."""
    conn = sqlite3.connect('expenses.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount REAL,
            category TEXT,
            timestamp DATETIME
        )
    ''')
    conn.commit()
    conn.close()

def save_expense(user_id: int, amount: float, category: str):
    """Saves a single expense entry to the database."""
    conn = sqlite3.connect('expenses.db')
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO expenses (user_id, amount, category, timestamp) VALUES (?, ?, ?, ?)",
        (user_id, amount, category, datetime.now())
    )
    conn.commit()
    conn.close()

def get_total_spending(user_id: int):
    """Retrieves total spending grouped by category for a specific user."""
    conn = sqlite3.connect('expenses.db')
    cursor = conn.cursor()
    cursor.execute(
        "SELECT category, SUM(amount) FROM expenses WHERE user_id = ? GROUP BY category",
        (user_id,)
    )
    results = cursor.fetchall()
    conn.close()
    return results

# -------------------------------
# AI Parsing Logic
# -------------------------------
def parse_expenses_with_gemini(message: str) -> list:
    prompt = f"""
    Extract all individual expenses from the message below.
    Message: "{message}"
    """
    try:
        # Using the explicit model path
        model = genai.GenerativeModel(model_name="gemini-2.5-flash-lite")
        
        response = model.generate_content(
            prompt,
            generation_config=genai.types.GenerationConfig(
                temperature=0.1,
                response_mime_type="application/json",
                # This ensures the output is ALWAYS a list of amount/category objects
                response_schema={
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "amount": {"type": "number"},
                            "category": {"type": "string"}
                        },
                        "required": ["amount", "category"]
                    }
                }
            )
        )
        
        return json.loads(response.text)
    except Exception as e:
        logger.error(f"AI Parsing Error: {e}")
        return []

# -------------------------------
# Command Handlers
# -------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! I'm your Finance Bot. 💸\n\n"
        "Tell me your expenses (e.g., 'Coffee 5, Lunch 15, and 50 for gas') "
        "and I will automatically categorize and save them with a timestamp.\n\n"
        "Use /summary to see your total spending!"
    )

async def summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    totals = get_total_spending(user_id)
    
    if not totals:
        await update.message.reply_text("You haven't recorded any expenses yet!")
        return

    report = "📊 *Your Spending Summary:*\n"
    grand_total = 0
    for category, amount in totals:
        report += f"• {category}: ${amount:.2f}\n"
        grand_total += amount
    
    report += f"\n💰 *Grand Total: ${grand_total:.2f}*"
    await update.message.reply_text(report, parse_mode='Markdown')

# -------------------------------
# Message Handler
# -------------------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.message.from_user.id
    
    # Handle group mentions
    if update.message.chat.type in ['group', 'supergroup'] and BOT_HANDLE in text:
        text = text.replace(BOT_HANDLE, '').strip()

    # Inform the user you're thinking
    await update.message.reply_chat_action("typing")
    
    expenses = parse_expenses_with_gemini(text)

    if not expenses:
        await update.message.reply_text("I couldn't identify any expenses. Try: 'Starbucks 6.50'")
        return

    responses = []
    for item in expenses:
        amt = item.get('amount')
        cat = item.get('category', 'Misc')
        
        if amt:
            save_expense(user_id, amt, cat)
            responses.append(f"✅ Saved: ${amt} in {cat}")

    await update.message.reply_text("\n".join(responses))

# -------------------------------
# Main Execution
# -------------------------------
def main():
    # 1. Initialize DB table
    init_db()

    # 2. Build the Application
    app = Application.builder().token(API_TOKEN).build()

    # 3. Add Handlers
    app.add_handler(CommandHandler('start', start_command))
    app.add_handler(CommandHandler('summary', summary_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # 4. Start the Bot
    print("Bot is running... Press Ctrl+C to stop.")
    app.run_polling(poll_interval=1)

if __name__ == "__main__":
    main()