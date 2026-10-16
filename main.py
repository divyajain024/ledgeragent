import os
import sqlite3
import traceback
import pandas as pd
import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from openai import OpenAI

app = FastAPI()

API_BASE = "https://exam.sanand.workers.dev/questionData?email=22f2000901%40ds.study.iitm.ac.in&quizSign=SDjaPcG3ZnarUrzUDzCzJewltMiS718GRDkDguC8476R7LgQjmNnDH3W2JvPzsUl0%2F812fT7dWWAGZR6tXQX9coerwL7yWUbwiUQuanxaz%2F3SHgNnAF4XonVA%2FE5xOyTuOtfOAdgxdppPXLuTLIzRI7k1Kh8o4s6Ss5yYY0wOgaDlw%2BqZH0bTZ3QUohERul61kO%2FNZnEwV901dE1fMWdfPwunG1goBzBlZuhUsR8Q%2B%2FF1aqJNKsPGHF2C0h7GimDYnT28iNawW0oOMcqcebWXUXD5f3dP8KEMW5BieJR%2BCLQRZ49FfuKHVmCy0S5ccAScSjWBWHPIlCBpFBqBfJsEw%3D%3D&questionId=q-ledger-agent-server"

# Ensure base_url has proper protocol fallback
base_url = os.environ.get("OPENAI_BASE_URL", "https://aipipe.org/openai/v1")
if not base_url.startswith("http://") and not base_url.startswith("https://"):
    base_url = "https://" + base_url

client = OpenAI(
    api_key=os.environ.get("OPENAI_API_KEY"),
    base_url=base_url
)

def load_data_into_db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    try:
        base_res = requests.get(f"{API_BASE}&path=/").json()
        links = base_res.get("links", {})
        export_url = links.get("export")
        orders_url = links.get("orders")
        rates_url = links.get("rates")

        orders_data = []
        if export_url:
            try:
                res = requests.get(export_url).json()
                if isinstance(res, list):
                    orders_data = res
                elif isinstance(res, dict):
                    for k, v in res.items():
                        if isinstance(v, list):
                            orders_data = v
                            break
            except Exception as e:
                print(f"Export fetch failed: {e}")

        if not orders_data and orders_url:
            current_url = orders_url
            page = 1
            while current_url and page <= 20:
                res = requests.get(current_url).json()
                if isinstance(res, list):
                    orders_data.extend(res)
                    break
                elif isinstance(res, dict):
                    for k, v in res.items():
                        if isinstance(v, list):
                            orders_data.extend(v)
                            break
                    next_url = res.get("next") or res.get("next_page")
                    if next_url and next_url != current_url:
                        current_url = next_url
                        page += 1
                    else:
                        break
                else:
                    break

        df_orders = pd.DataFrame(orders_data) if orders_data else pd.DataFrame()

        rates_data = []
        if rates_url:
            try:
                res = requests.get(rates_url).json()
                if isinstance(res, list):
                    rates_data = res
                elif isinstance(res, dict):
                    for k, v in res.items():
                        if isinstance(v, list):
                            rates_data = v
                            break
                    if not rates_data:
                        rates_data = [{"currency": k, "rate": v} for k, v in res.items() if isinstance(v, (int, float))]
            except Exception as e:
                print(f"Rates fetch failed: {e}")

        df_rates = pd.DataFrame(rates_data) if rates_data else pd.DataFrame([{"currency": "USD", "rate": 1.0}])

        if not df_orders.empty:
            df_orders.columns = [str(c).strip().replace(" ", "_").replace("-", "_").replace(".", "_") for c in df_orders.columns]
        if not df_rates.empty:
            df_rates.columns = [str(c).strip().replace(" ", "_").replace("-", "_").replace(".", "_") for c in df_rates.columns]

        if not df_orders.empty and "order_id" in df_orders.columns and "updated_at" in df_orders.columns:
            df_orders["updated_at"] = pd.to_datetime(df_orders["updated_at"], errors="coerce")
            df_orders = df_orders.sort_values("updated_at").drop_duplicates(subset=["order_id"], keep="last")

        df_orders.to_sql("orders", conn, index=False, if_exists="replace")
        df_rates.to_sql("rates", conn, index=False, if_exists="replace")
        print("Successfully loaded orders and rates into SQLite.")
    except Exception as e:
        print(f"Error in load_data_into_db: {e}")
        traceback.print_exc()

    return conn

db_conn = load_data_into_db()

class QueryRequest(BaseModel):
    question: str

@app.post("/")
def answer_question(req: QueryRequest):
    try:
        schema_df = pd.read_sql("SELECT sql FROM sqlite_master WHERE type='table';", db_conn)
        schema_info = "\n".join(schema_df['sql'].tolist()) if not schema_df.empty else "No tables found."

        sample_orders = pd.read_sql("SELECT * FROM orders LIMIT 3;", db_conn).to_string() if "orders" in pd.read_sql("SELECT name FROM sqlite_master WHERE type='table';", db_conn)['name'].values else ""
        sample_rates = pd.read_sql("SELECT * FROM rates LIMIT 3;", db_conn).to_string() if "rates" in pd.read_sql("SELECT name FROM sqlite_master WHERE type='table';", db_conn)['name'].values else ""

        prompt = f"""
You are an expert SQL analyst for Acme Appliances order ledger.
Database Schema:
{schema_info}

Sample Orders:
{sample_orders}

Sample Rates:
{sample_rates}

Rules & Context:
- Business dates use Asia/Kolkata timezone.
- An order id can appear more than once; the row with the latest updated_at is current (already deduplicated in 'orders' table).
- Only orders with status 'paid' count as revenue.
- Money is in USD, correct to the cent (round to 2 decimals). If amounts are in other currencies, join with the 'rates' table to convert to USD.
- Product names as the API spells them.

Question: {req.question}

Task: Write a single, valid SQLite SQL query that answers this question. 
Return ONLY the SQL query inside a markdown code block (e.g. ```sql ... ```). No other text or explanation.
"""
        model_name = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        response = client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": prompt}],
            temperature=0
        )
        content = response.choices[0].message.content.strip()
        
        sql_query = content
        if "```sql" in content:
            sql_query = content.split("```sql")[1].split("```")[0].strip()
        elif "```" in content:
            sql_query = content.split("```")[1].split("```")[0].strip()

        print(f"Generated SQL: {sql_query}")

        result_df = pd.read_sql(sql_query, db_conn)
        
        if result_df.empty:
            answer = "0"
        elif len(result_df) == 1 and len(result_df.columns) == 1:
            val = result_df.iloc[0, 0]
            if isinstance(val, float):
                answer = round(val, 2)
            else:
                answer = val
        else:
            if len(result_df.columns) == 1:
                answer = result_df.iloc[:, 0].tolist()
            else:
                answer = result_df.to_dict(orient="records")

        return {"answer": answer}
    except Exception as e:
        error_msg = str(e)
        print(f"Error handling question '{req.question}': {error_msg}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=error_msg)
