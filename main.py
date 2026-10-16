import os
import sqlite3
import pandas as pd
import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from openai import OpenAI

app = FastAPI()

# Configuration using your exact URL parameters from the prompt
API_BASE = "https://exam.sanand.workers.dev/questionData?email=22f2000901%40ds.study.iitm.ac.in&quizSign=SDjaPcG3ZnarUrzUDzCzJewltMiS718GRDkDguC8476R7LgQjmNnDH3W2JvPzsUl0%2F812fT7dWWAGZR6tXQX9coerwL7yWUbwiUQuanxaz%2F3SHgNnAF4XonVA%2FE5xOyTuOtfOAdgxdppPXLuTLIzRI7k1Kh8o4s6Ss5yYY0wOgaDlw%2BqZH0bTZ3QUohERul61kO%2FNZnEwV901dE1fMWdfPwunG1goBzBlZuhUsR8Q%2B%2FF1aqJNKsPGHF2C0h7GimDYnT28iNawW0oOMcqcebWXUXD5f3dP8KEMW5BieJR%2BCLQRZ49FfuKHVmCy0S5ccAScSjWBWHPIlCBpFBqBfJsEw%3D%3D&questionId=q-ledger-agent-server"

# AI Pipe configuration (OpenAI compatible)
client = OpenAI(
    api_key=os.environ.get("OPENAI_API_KEY"),
    base_url=os.environ.get("OPENAI_BASE_URL", "https://aipipe.org/openai/v1")
)

def load_data_into_db():
    try:
        base_res = requests.get(f"{API_BASE}&path=/").json()
        orders_url = base_res["links"]["orders"]
        rates_url = base_res["links"]["rates"]

        orders_data = requests.get(orders_url).json()
        rates_data = requests.get(rates_url).json()

        # Handle different possible JSON payload structures safely
        if isinstance(orders_data, list):
            df_orders = pd.DataFrame(orders_data)
        elif isinstance(orders_data, dict):
            # Look for common key names or extract the first list found
            key = next((k for k, v in orders_data.items() if isinstance(v, list)), None)
            df_orders = pd.DataFrame(orders_data[key] if key else [])
        else:
            df_orders = pd.DataFrame()

        if isinstance(rates_data, list):
            df_rates = pd.DataFrame(rates_data)
        elif isinstance(rates_data, dict):
            key = next((k for k, v in rates_data.items() if isinstance(v, list)), None)
            df_rates = pd.DataFrame(rates_data[key] if key else [])
        else:
            df_rates = pd.DataFrame()

        # Sanitize column names for SQLite compatibility (replace spaces/special chars)
        df_orders.columns = [str(c).strip().replace(" ", "_").replace("-", "_").replace(".", "_") for c in df_orders.columns]
        df_rates.columns = [str(c).strip().replace(" ", "_").replace("-", "_").replace(".", "_") for c in df_rates.columns]

        # Apply Rule: latest updated_at per order_id is current
        if "order_id" in df_orders.columns and "updated_at" in df_orders.columns:
            df_orders["updated_at"] = pd.to_datetime(df_orders["updated_at"], errors="coerce")
            df_orders = df_orders.sort_values("updated_at").drop_duplicates(subset=["order_id"], keep="last")

        conn = sqlite3.connect(":memory:", check_same_thread=False)
        df_orders.to_sql("orders", conn, index=False, if_exists="replace")
        df_rates.to_sql("rates", conn, index=False, if_exists="replace")
        return conn
    except Exception as e:
        print(f"Error loading data into DB: {e}")
        raise e

db_conn = load_data_into_db()

class QueryRequest(BaseModel):
    question: str

@app.post("/")
def answer_question(req: QueryRequest):
    try:
        schema_query = "SELECT sql FROM sqlite_master WHERE type='table';"
        schema_df = pd.read_sql(schema_query, db_conn)
        schema_info = "\n".join(schema_df['sql'].tolist())

        prompt = f"""
You are an expert SQL analyst for Acme Appliances order ledger.
The database has these tables and schemas:
{schema_info}

Rules:
- Business dates use Asia/Kolkata timezone.
- Only orders with status 'paid' count as revenue.
- Money is in USD, correct to the cent (round to 2 decimals).
- Product names as the API spells them.
- Answer the user's question by writing a SQL query, executing it, and returning the direct final answer.

Question: {req.question}

Return ONLY the final answer value or text (e.g., number, product name, string). Do not include extra conversational filler.
"""
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0
        )
        answer = response.choices[0].message.content.strip()
        return {"answer": answer}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
