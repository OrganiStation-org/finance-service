import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel
from bson import ObjectId
from dotenv import load_dotenv

load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
DB_NAME     = os.getenv("DB_NAME", "organistation_finance")
PORT        = int(os.getenv("PORT", "8004"))
HOST        = os.getenv("HOST", "0.0.0.0")
INTERNAL_SERVICE_SECRET = os.getenv("INTERNAL_SERVICE_SECRET", "organistation_internal_secret")

client = None
db     = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global client, db
    client = AsyncIOMotorClient(MONGODB_URI)
    db = client[DB_NAME]
    await db.expenses.create_index("date")
    await db.invoices.create_index("client_name")
    await db.budgets.create_index("department")
    print(f"[Finance Service] Connected to MongoDB: {DB_NAME}")
    yield
    client.close()

app = FastAPI(title="OrganiStation – Finance Service", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

def oid(doc):
    doc["id"] = doc["_id"] = str(doc["_id"])
    return doc

# ── Schemas ────────────────────────────────────────────────────────────────────

class Expense(BaseModel):
    title:       str
    amount:      float
    category:    str = "Operations"
    date:        Optional[str] = None
    notes:       Optional[str] = None
    submitted_by:Optional[str] = None
    status:      str = "pending"     # pending | approved | rejected

class ExpenseUpdate(BaseModel):
    title:    Optional[str]   = None
    amount:   Optional[float] = None
    category: Optional[str]   = None
    status:   Optional[str]   = None
    notes:    Optional[str]   = None

class Budget(BaseModel):
    department:  str
    amount:      float
    period:      str = "monthly"
    year:        Optional[int] = None
    month:       Optional[int] = None
    notes:       Optional[str] = None

class Invoice(BaseModel):
    client_name: str
    amount:      float
    description: Optional[str] = None
    due_date:    Optional[str] = None
    status:      str = "pending"     # pending | paid | overdue | cancelled

class InvoiceUpdate(BaseModel):
    client_name: Optional[str]   = None
    amount:      Optional[float] = None
    status:      Optional[str]   = None
    description: Optional[str]   = None
    due_date:    Optional[str]   = None

class PurgeUserRequest(BaseModel):
    email:      str
    first_name: Optional[str] = None
    last_name:  Optional[str] = None

def _verify_internal(x_internal_secret: Optional[str]):
    if x_internal_secret != INTERNAL_SERVICE_SECRET:
        raise HTTPException(403, "Forbidden")

def _user_match(email: str, first_name: Optional[str], last_name: Optional[str]):
    values = [email]
    full_name = " ".join(part for part in [first_name, last_name] if part).strip()
    if full_name:
        values.append(full_name)
    return {"$in": values}

# ── Health ─────────────────────────────────────────────────────────────────────

@app.get("/")
@app.get("/health")
@app.get("/api/health")
async def health():
    return {"status": "healthy", "service": "finance-service"}

# ── Summary ────────────────────────────────────────────────────────────────────

@app.get("/api/summary")
async def get_summary():
    total_expenses = 0.0
    async for e in db.expenses.find({"status": {"$ne": "rejected"}}):
        total_expenses += e.get("amount", 0)

    total_revenue = 0.0
    pending_invoices = 0
    async for i in db.invoices.find():
        if i.get("status") == "paid":
            total_revenue += i.get("amount", 0)
        elif i.get("status") == "pending":
            pending_invoices += 1

    return {
        "total_expenses":   round(total_expenses, 2),
        "total_revenue":    round(total_revenue, 2),
        "pending_invoices": pending_invoices,
        "net_balance":      round(total_revenue - total_expenses, 2),
    }

# ── Expenses ───────────────────────────────────────────────────────────────────

@app.get("/api/expenses")
async def list_expenses(submitted_by: Optional[str] = None):
    query = {}
    if submitted_by:
        query["submitted_by"] = submitted_by
    return [oid(e) async for e in db.expenses.find(query).sort("date", -1)]

@app.post("/api/expenses", status_code=201)
async def create_expense(exp: Expense):
    doc = exp.model_dump()
    doc["created_at"] = datetime.utcnow()
    if not doc.get("date"):
        doc["date"] = datetime.utcnow().strftime("%Y-%m-%d")
    r = await db.expenses.insert_one(doc)
    doc["id"] = doc["_id"] = str(r.inserted_id)
    return doc

@app.put("/api/expenses/{eid}")
async def update_expense(eid: str, upd: ExpenseUpdate):
    data = {k: v for k, v in upd.model_dump().items() if v is not None}
    data["updated_at"] = datetime.utcnow()
    await db.expenses.update_one({"_id": ObjectId(eid)}, {"$set": data})
    e = await db.expenses.find_one({"_id": ObjectId(eid)})
    if not e: raise HTTPException(404, "Expense not found")
    return oid(e)

@app.delete("/api/expenses/{eid}")
async def delete_expense(eid: str):
    r = await db.expenses.delete_one({"_id": ObjectId(eid)})
    if r.deleted_count == 0: raise HTTPException(404, "Expense not found")
    return {"message": "Expense deleted"}

# ── Budgets ────────────────────────────────────────────────────────────────────

@app.get("/api/budgets")
async def list_budgets():
    return [oid(b) async for b in db.budgets.find()]

@app.post("/api/budgets", status_code=201)
async def create_budget(budget: Budget):
    doc = budget.model_dump()
    doc["created_at"] = datetime.utcnow()
    if not doc.get("year"):
        doc["year"] = datetime.utcnow().year
    r = await db.budgets.insert_one(doc)
    doc["id"] = doc["_id"] = str(r.inserted_id)
    return doc

# ── Invoices ───────────────────────────────────────────────────────────────────

@app.get("/api/invoices")
async def list_invoices():
    return [oid(i) async for i in db.invoices.find().sort("due_date", 1)]

@app.post("/api/invoices", status_code=201)
async def create_invoice(inv: Invoice):
    doc = inv.model_dump()
    doc["created_at"] = doc["updated_at"] = datetime.utcnow()
    r = await db.invoices.insert_one(doc)
    doc["id"] = doc["_id"] = str(r.inserted_id)
    return doc

@app.put("/api/invoices/{iid}")
async def update_invoice(iid: str, upd: InvoiceUpdate):
    data = {k: v for k, v in upd.model_dump().items() if v is not None}
    data["updated_at"] = datetime.utcnow()
    await db.invoices.update_one({"_id": ObjectId(iid)}, {"$set": data})
    i = await db.invoices.find_one({"_id": ObjectId(iid)})
    if not i: raise HTTPException(404, "Invoice not found")
    return oid(i)

@app.delete("/api/invoices/{iid}")
async def delete_invoice(iid: str):
    r = await db.invoices.delete_one({"_id": ObjectId(iid)})
    if r.deleted_count == 0: raise HTTPException(404, "Invoice not found")
    return {"message": "Invoice deleted"}

@app.post("/api/internal/purge-user")
async def purge_user(
    body: PurgeUserRequest,
    x_internal_secret: Optional[str] = Header(None, alias="X-Internal-Secret"),
):
    _verify_internal(x_internal_secret)
    match = _user_match(body.email, body.first_name, body.last_name)
    expenses_deleted = (await db.expenses.delete_many({"submitted_by": match})).deleted_count
    return {"expenses_deleted": expenses_deleted}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host=HOST, port=PORT, reload=True)
