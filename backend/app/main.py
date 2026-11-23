from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import quote
import json, os, secrets, re
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Depends, HTTPException, Response, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from jose import jwt, JWTError
from pwdlib import PasswordHash
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, and_, create_engine, or_
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker, Session

DATABASE_URL = os.getenv('DATABASE_URL', 'postgresql+psycopg://kitchen:kitchen@db:5432/kitchen')
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql+psycopg://', 1)
elif DATABASE_URL.startswith('postgresql://'):
    DATABASE_URL = DATABASE_URL.replace('postgresql://', 'postgresql+psycopg://', 1)
SECRET_KEY = os.getenv('SECRET_KEY', 'replace-this-secret-before-deploying')
COOKIE_SECURE = os.getenv('COOKIE_SECURE', 'false').lower() == 'true'
TOKEN_TTL_HOURS = int(os.getenv('TOKEN_TTL_HOURS', '12'))
KITCHEN_TZ = ZoneInfo(os.getenv('KITCHEN_TIMEZONE', 'Asia/Krasnoyarsk'))
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
hasher = PasswordHash.recommended()

def kitchen_today() -> date:
    return datetime.now(KITCHEN_TZ).date()

class Base(DeclarativeBase): pass

class User(Base):
    __tablename__='users'
    id: Mapped[int]=mapped_column(primary_key=True)
    name: Mapped[str]=mapped_column(String(120))
    login: Mapped[str]=mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str]=mapped_column(String(255))
    role: Mapped[str]=mapped_column(String(20), index=True)
    active: Mapped[bool]=mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool]=mapped_column(Boolean, default=False)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class Supplier(Base):
    __tablename__='suppliers'
    id: Mapped[int]=mapped_column(primary_key=True)
    name: Mapped[str]=mapped_column(String(200), unique=True, index=True)
    active: Mapped[bool]=mapped_column(Boolean, default=True)

class Product(Base):
    __tablename__='products'
    id: Mapped[int]=mapped_column(primary_key=True)
    name: Mapped[str]=mapped_column(String(240), index=True)
    category: Mapped[str]=mapped_column(String(160), index=True)
    unit: Mapped[Optional[str]]=mapped_column(String(40), nullable=True)
    min_stock: Mapped[Optional[float]]=mapped_column(Float, nullable=True)
    supplier_id: Mapped[Optional[int]]=mapped_column(ForeignKey('suppliers.id'), nullable=True, index=True)
    source_supplier_label: Mapped[Optional[str]]=mapped_column(String(240), nullable=True)
    needs_supplier: Mapped[bool]=mapped_column(Boolean, default=False, index=True)
    active: Mapped[bool]=mapped_column(Boolean, default=True, index=True)
    source_sheet: Mapped[Optional[str]]=mapped_column(String(80), nullable=True)
    terminal_order: Mapped[Optional[int]]=mapped_column(Integer, nullable=True, index=True)
    terminal_category: Mapped[Optional[str]]=mapped_column(String(160), nullable=True)
    supplier: Mapped[Optional[Supplier]]=relationship()

class Order(Base):
    __tablename__='orders'
    id: Mapped[int]=mapped_column(primary_key=True)
    author_id: Mapped[int]=mapped_column(ForeignKey('users.id'), index=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    planned_date: Mapped[Optional[date]]=mapped_column(Date, nullable=True, index=True)
    status: Mapped[str]=mapped_column(String(30), default='Отправлена', index=True)
    is_approved: Mapped[bool]=mapped_column(Boolean, default=False, nullable=False, index=True)
    requires_revision: Mapped[bool]=mapped_column(Boolean, default=False, nullable=False, index=True)
    review_note: Mapped[Optional[str]]=mapped_column(Text, nullable=True)
    comment: Mapped[Optional[str]]=mapped_column(Text, nullable=True)
    author: Mapped[User]=relationship()
    items: Mapped[list['OrderItem']]=relationship(cascade='all, delete-orphan', order_by='OrderItem.id')

class OrderItem(Base):
    __tablename__='order_items'
    id: Mapped[int]=mapped_column(primary_key=True)
    order_id: Mapped[int]=mapped_column(ForeignKey('orders.id', ondelete='CASCADE'), index=True)
    product_id: Mapped[Optional[int]]=mapped_column(ForeignKey('products.id'), nullable=True)
    product_name: Mapped[str]=mapped_column(String(240))
    category: Mapped[str]=mapped_column(String(160))
    supplier_name: Mapped[Optional[str]]=mapped_column(String(240), nullable=True)
    quantity: Mapped[float]=mapped_column(Float)
    unit: Mapped[Optional[str]]=mapped_column(String(40), nullable=True)
    comment: Mapped[Optional[str]]=mapped_column(Text, nullable=True)
    status: Mapped[str]=mapped_column(String(30), default='К заказу', index=True)
    product: Mapped[Optional[Product]]=relationship()

class DailyChecklist(Base):
    __tablename__='daily_checklists'
    __table_args__=(UniqueConstraint('author_id','planned_date',name='uq_daily_checklists_author_date'),)
    id: Mapped[int]=mapped_column(primary_key=True)
    author_id: Mapped[int]=mapped_column(ForeignKey('users.id'), index=True)
    planned_date: Mapped[date]=mapped_column(Date, index=True)
    submitted_order_id: Mapped[Optional[int]]=mapped_column(ForeignKey('orders.id'), nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    completed_at: Mapped[Optional[datetime]]=mapped_column(DateTime(timezone=True), nullable=True)
    author: Mapped[User]=relationship()
    entries: Mapped[list['DailyChecklistEntry']]=relationship(cascade='all, delete-orphan', order_by='DailyChecklistEntry.sort_order, DailyChecklistEntry.id')

class DailyChecklistEntry(Base):
    __tablename__='daily_checklist_entries'
    __table_args__=(UniqueConstraint('checklist_id','sort_order',name='uq_checklist_sort_order'),)
    id: Mapped[int]=mapped_column(primary_key=True)
    checklist_id: Mapped[int]=mapped_column(ForeignKey('daily_checklists.id',ondelete='CASCADE'), index=True)
    product_id: Mapped[int]=mapped_column(ForeignKey('products.id'), index=True)
    sort_order: Mapped[int]=mapped_column(Integer, index=True)
    category: Mapped[Optional[str]]=mapped_column(String(160), nullable=True)
    state: Mapped[str]=mapped_column(String(20), default='pending', index=True)
    quantity: Mapped[Optional[float]]=mapped_column(Float, nullable=True)
    unit: Mapped[Optional[str]]=mapped_column(String(40), nullable=True)
    comment: Mapped[Optional[str]]=mapped_column(Text, nullable=True)
    product: Mapped[Product]=relationship()

class LoginIn(BaseModel): login: str; password: str
class UserIn(BaseModel): name: str; login: str; password: str = Field(min_length=8); role: str
class UserUpdate(BaseModel): name: str; role: str; active: bool=True; password: Optional[str]=None
class PasswordChangeIn(BaseModel): current_password: str; new_password: str = Field(min_length=8)
class ProductIn(BaseModel): name: str; category: str; unit: Optional[str]=None; min_stock: Optional[float]=None; supplier_id: Optional[int]=None; active: bool=True
class SupplierIn(BaseModel): name: str; active: bool=True
class ItemIn(BaseModel): product_id: int; quantity: float=Field(gt=0); unit: Optional[str]=Field(default=None, max_length=40); comment: Optional[str]=None
class OrderIn(BaseModel): items: list[ItemIn]=Field(min_length=1); planned_date: date=Field(default_factory=kitchen_today); comment: Optional[str]=None
class ItemStatusIn(BaseModel): status: str
class OrderUpdate(BaseModel): items: list[ItemIn]=Field(min_length=1); planned_date: date; comment: Optional[str]=None
class OrderApprovalIn(BaseModel): is_approved: bool
class OrderReturnIn(BaseModel): comment: str=Field(min_length=1,max_length=1000)
class ChecklistEntryIn(BaseModel): state: str; quantity: Optional[float]=Field(default=None,gt=0); unit: Optional[str]=Field(default=None,max_length=40); comment: Optional[str]=Field(default=None,max_length=1000)

app=FastAPI(title='Kitchen Orders API', version='1.0.0')
app.add_middleware(CORSMiddleware, allow_origins=os.getenv('CORS_ORIGINS','http://localhost:8080').split(','), allow_credentials=True, allow_methods=['*'], allow_headers=['*'])

def get_db():
    db=SessionLocal()
    try: yield db
    finally: db.close()

def user_json(u: User): return {'id':u.id,'name':u.name,'login':u.login,'role':u.role,'active':u.active,'must_change_password':u.must_change_password,'created_at':u.created_at.isoformat() if u.created_at else None}
def product_json(p: Product): return {'id':p.id,'name':p.name,'category':p.category,'unit':p.unit,'min_stock':p.min_stock,'supplier_id':p.supplier_id,'supplier_name':p.supplier.name if p.supplier else None,'source_supplier_label':p.source_supplier_label,'needs_supplier':p.needs_supplier,'active':p.active,'terminal_order':p.terminal_order,'terminal_category':p.terminal_category}
def order_kitchen_date(o: Order) -> date:
    if o.planned_date:
        return o.planned_date
    created = o.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return created.astimezone(KITCHEN_TZ).date()

def validate_planned_date(planned: date, previous: Optional[date]=None):
    if planned < kitchen_today() and planned != previous:
        raise HTTPException(400, 'Дата закупки должна быть сегодня или позже')

def order_json(o: Order):
    return {'id':o.id,'author_id':o.author_id,'author_name':o.author.name,'created_at':o.created_at.isoformat(),'planned_date':order_kitchen_date(o).isoformat(),'status':o.status,'is_approved':o.is_approved,'requires_revision':o.requires_revision,'review_note':o.review_note,'comment':o.comment,'items':[{'id':i.id,'product_id':i.product_id,'name':i.product_name,'category':i.category,'supplier_name':i.supplier_name,'quantity':i.quantity,'unit':i.unit,'comment':i.comment,'status':i.status} for i in o.items]}

def checklist_entry_json(entry: DailyChecklistEntry):
    p=entry.product
    return {'id':entry.id,'product_id':p.id,'name':p.name,'category':entry.category or p.terminal_category or p.category,'supplier_name':p.supplier.name if p.supplier else (p.source_supplier_label or None),'min_stock':p.min_stock,'default_unit':p.unit,'state':entry.state,'quantity':entry.quantity,'unit':entry.unit or p.unit,'comment':entry.comment,'sort_order':entry.sort_order,'active':p.active}

def checklist_json(checklist: DailyChecklist):
    entries=[checklist_entry_json(entry) for entry in checklist.entries]
    return {'id':checklist.id,'planned_date':checklist.planned_date.isoformat(),'completed':checklist.completed_at is not None,'submitted_order_id':checklist.submitted_order_id,'total':len(entries),'reviewed':sum(e['state']!='pending' for e in entries),'entries':entries}

def ensure_checklist(db: Session, user: User, planned: date) -> DailyChecklist:
    checklist=db.query(DailyChecklist).filter_by(author_id=user.id,planned_date=planned).first()
    if not checklist:
        checklist=DailyChecklist(author_id=user.id,planned_date=planned)
        db.add(checklist);db.flush()
    if checklist.completed_at is None:
        present={entry.sort_order for entry in checklist.entries}
        terminal_path=Path(__file__).resolve().parents[1]/'terminal_catalog.json'
        if not terminal_path.exists(): terminal_path=Path(__file__).resolve().parents[2]/'data'/'terminal_catalog.json'
        terminal=json.loads(terminal_path.read_text(encoding='utf-8')) if terminal_path.exists() else []
        products=db.query(Product).filter(Product.active.is_(True)).order_by(Product.id).all()
        by_name={}
        for p in products:by_name.setdefault(p.name.casefold(),[]).append(p)
        terminal_names={item['name'].casefold() for item in terminal}
        used={}
        for item in terminal:
            slot=item['terminal_order'];name=item['name'].casefold()
            if slot in present:continue
            candidates=by_name.get(name,[])
            if not candidates:continue
            category=item['category'].casefold()
            matches=[p for p in candidates if p.category.casefold()==category or (p.terminal_category and p.terminal_category.casefold()==category)]
            source=matches or candidates
            index=used.get((name,category),0);p=source[min(index,len(source)-1)];used[(name,category)]=index+1
            checklist.entries.append(DailyChecklistEntry(product_id=p.id,sort_order=slot,category=item['category'],state='pending'))
            present.add(slot)
        for p in products:
            slot=1000000+p.id
            if p.source_sheet is None and p.name.casefold() not in terminal_names and slot not in present:
                checklist.entries.append(DailyChecklistEntry(product_id=p.id,sort_order=slot,category=p.category,state='pending'))
                present.add(slot)
    db.flush()
    return checklist

from fastapi import Request
def current_user(request: Request, db: Session=Depends(get_db)) -> User:
    token=request.cookies.get('kitchen_session')
    if not token: raise HTTPException(401,'Войдите в систему')
    try: payload=jwt.decode(token, SECRET_KEY, algorithms=['HS256']); uid=int(payload['sub'])
    except (JWTError,KeyError,ValueError): raise HTTPException(401,'Сессия завершилась. Войдите снова')
    u=db.get(User,uid)
    if not u or not u.active: raise HTTPException(401,'Учётная запись отключена')
    return u

def require_role(*roles):
    def check(u: User=Depends(current_user)):
        if u.role not in roles: raise HTTPException(403,'Недостаточно прав')
        return u
    return check

@app.on_event('startup')
def init_db():
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        if not db.query(User).filter(User.role=='chef').first():
            db.add(User(name=os.getenv('ADMIN_NAME','Шеф-повар'),login=os.getenv('ADMIN_LOGIN','admin'),password_hash=hasher.hash(os.getenv('ADMIN_PASSWORD','ChangeMe123!')),role='chef',active=True))
        seed_path=Path(__file__).resolve().parents[1]/'seed.json'
        if seed_path.exists():
            data=json.loads(seed_path.read_text(encoding='utf-8'))
            supplier_by_name={s.name:s for s in db.query(Supplier).all()}
            for name in data.get('suppliers',[]):
                if name not in supplier_by_name:
                    supplier_by_name[name]=Supplier(name=name);db.add(supplier_by_name[name])
            db.flush()
            existing_counts={}
            for p in db.query(Product).all():existing_counts[p.name]=existing_counts.get(p.name,0)+1
            seed_counts={}
            for raw in data.get('products',[]):seed_counts[raw['name']]=seed_counts.get(raw['name'],0)+1
            for raw in data.get('products',[]):
                if existing_counts.get(raw['name'],0)>=seed_counts[raw['name']]: continue
                label=raw.get('supplier_name'); mapped=bool(label and label in supplier_by_name and not raw.get('needs_supplier'))
                db.add(Product(name=raw['name'],category=raw['category'],unit=raw.get('unit'),min_stock=raw.get('min_stock'),supplier_id=supplier_by_name[label].id if mapped else None,source_supplier_label=label,needs_supplier=not mapped,source_sheet=raw.get('source'),terminal_order=raw.get('terminal_order'),terminal_category=raw.get('terminal_category')))
                existing_counts[raw['name']]=existing_counts.get(raw['name'],0)+1
        db.commit()

@app.get('/api/health')
def health(db: Session=Depends(get_db)):
    db.execute(__import__('sqlalchemy').text('SELECT 1')); return {'status':'ok'}

@app.post('/api/auth/login')
def login(body: LoginIn, response: Response, db: Session=Depends(get_db)):
    u=db.query(User).filter(User.login==body.login).first()
    if not u or not u.active or not hasher.verify(body.password,u.password_hash): raise HTTPException(401,'Неверный логин или пароль')
    token=jwt.encode({'sub':str(u.id),'exp':datetime.now(timezone.utc)+timedelta(hours=TOKEN_TTL_HOURS)},SECRET_KEY,algorithm='HS256')
    response.set_cookie('kitchen_session',token,httponly=True,secure=COOKIE_SECURE,samesite='lax',max_age=TOKEN_TTL_HOURS*3600,path='/')
    return {'user':user_json(u)}

@app.post('/api/auth/logout')
def logout(response: Response): response.delete_cookie('kitchen_session',path='/'); return {'ok':True}

@app.get('/api/auth/me')
def me(u: User=Depends(current_user)): return user_json(u)

@app.post('/api/auth/change-password')
def change_password(body: PasswordChangeIn, db: Session=Depends(get_db), u: User=Depends(current_user)):
    if not hasher.verify(body.current_password,u.password_hash): raise HTTPException(400,'Текущий пароль введён неверно')
    u.password_hash=hasher.hash(body.new_password); u.must_change_password=False
    db.commit(); db.refresh(u)
    return user_json(u)

@app.get('/api/products')
def products(q: str='', category: str='', needs_supplier: Optional[bool]=None, include_inactive: bool=False, db: Session=Depends(get_db), u: User=Depends(current_user)):
    query=db.query(Product)
    if not (include_inactive and u.role=='chef'): query=query.filter(Product.active.is_(True))
    if q.strip(): query=query.filter(Product.name.ilike(f'%{q.strip()}%'))
    if category: query=query.filter(Product.category==category)
    if needs_supplier is not None: query=query.filter(Product.needs_supplier.is_(needs_supplier))
    return [product_json(p) for p in query.order_by(Product.category,Product.name).all()]

@app.get('/api/categories')
def categories(db: Session=Depends(get_db), u: User=Depends(current_user)):
    return [r[0] for r in db.query(Product.category).filter(Product.active.is_(True)).distinct().order_by(Product.category).all()]

@app.post('/api/products')
def create_product(body: ProductIn, db: Session=Depends(get_db), u: User=Depends(require_role('chef'))):
    if body.supplier_id and not db.get(Supplier,body.supplier_id): raise HTTPException(400,'Поставщик не найден')
    p=Product(**body.model_dump(),needs_supplier=body.supplier_id is None)
    db.add(p); db.commit(); db.refresh(p); return product_json(p)

@app.put('/api/products/{product_id}')
def update_product(product_id: int, body: ProductIn, db: Session=Depends(get_db), u: User=Depends(require_role('chef'))):
    p=db.get(Product,product_id)
    if not p: raise HTTPException(404,'Товар не найден')
    if body.supplier_id and not db.get(Supplier,body.supplier_id): raise HTTPException(400,'Поставщик не найден')
    for k,v in body.model_dump().items(): setattr(p,k,v)
    if p.terminal_order is not None: p.terminal_category=body.category
    p.needs_supplier=body.supplier_id is None; p.source_supplier_label=None
    db.commit(); db.refresh(p); return product_json(p)

@app.get('/api/checklists/{planned_date}')
def get_daily_checklist(planned_date: date, db: Session=Depends(get_db), u: User=Depends(require_role('souschef'))):
    validate_planned_date(planned_date)
    checklist=ensure_checklist(db,u,planned_date)
    db.commit();db.refresh(checklist)
    return checklist_json(checklist)

@app.patch('/api/checklists/{planned_date}/entries/{entry_id}')
def update_checklist_entry(planned_date: date, entry_id: int, body: ChecklistEntryIn, db: Session=Depends(get_db), u: User=Depends(require_role('souschef'))):
    checklist=db.query(DailyChecklist).filter_by(author_id=u.id,planned_date=planned_date).first()
    if not checklist: raise HTTPException(404,'Чек-лист не найден')
    if checklist.completed_at: raise HTTPException(400,'Список уже завершён')
    entry=db.query(DailyChecklistEntry).filter_by(id=entry_id,checklist_id=checklist.id).first()
    if not entry: raise HTTPException(404,'Позиция не найдена в этом списке')
    next_unreviewed=db.query(DailyChecklistEntry).filter(DailyChecklistEntry.checklist_id==checklist.id,DailyChecklistEntry.state=='pending').order_by(DailyChecklistEntry.sort_order).first()
    if next_unreviewed and entry.sort_order>next_unreviewed.sort_order: raise HTTPException(400,'Сначала пройдите предыдущие позиции списка')
    if body.state not in ('pending','skip','quantity'): raise HTTPException(400,'Выберите количество или прочерк')
    if body.state=='quantity' and (body.quantity is None or body.quantity<=0): raise HTTPException(400,'Введите количество больше нуля')
    entry.state=body.state
    entry.quantity=body.quantity if body.state=='quantity' else None
    entry.unit=(body.unit.strip() if body.unit and body.unit.strip() else None) if body.state=='quantity' else None
    entry.comment=(body.comment.strip() if body.comment and body.comment.strip() else None) if body.state=='quantity' else None
    db.commit();db.refresh(entry)
    return checklist_entry_json(entry)

@app.post('/api/checklists/{planned_date}/skip-remaining')
def skip_remaining_checklist_entries(planned_date: date, db: Session=Depends(get_db), u: User=Depends(require_role('souschef'))):
    validate_planned_date(planned_date)
    checklist=db.query(DailyChecklist).filter_by(author_id=u.id,planned_date=planned_date).first()
    if not checklist: raise HTTPException(404,'Чек-лист не найден')
    if checklist.completed_at: raise HTTPException(400,'Список уже завершён')
    pending=db.query(DailyChecklistEntry).filter_by(checklist_id=checklist.id,state='pending').all()
    for entry in pending:
        entry.state='skip';entry.quantity=None;entry.unit=None;entry.comment=None
    db.commit();db.refresh(checklist)
    return checklist_json(checklist)

@app.post('/api/checklists/{planned_date}/submit')
def submit_daily_checklist(planned_date: date, db: Session=Depends(get_db), u: User=Depends(require_role('souschef'))):
    validate_planned_date(planned_date)
    checklist=ensure_checklist(db,u,planned_date)
    pending=sum(entry.state=='pending' for entry in checklist.entries)
    if pending: raise HTTPException(400,f'Отметьте все позиции: осталось {pending}')
    if checklist.completed_at:
        order=db.get(Order,checklist.submitted_order_id) if checklist.submitted_order_id else None
        return {'checklist':checklist_json(checklist),'order':order_json(order) if order else None}
    order=None
    selected=[entry for entry in checklist.entries if entry.state=='quantity']
    if selected:
        order=Order(author_id=u.id,planned_date=planned_date,status='Отправлена',is_approved=False)
        db.add(order);db.flush()
        for entry in selected:
            p=db.get(Product,entry.product_id)
            if not p or not p.active: raise HTTPException(400,'Одна из выбранных позиций больше недоступна. Обновите список.')
            order.items.append(OrderItem(product_id=p.id,product_name=p.name,category=entry.category or p.terminal_category or p.category,supplier_name=p.supplier.name if p.supplier else (p.source_supplier_label or None),quantity=entry.quantity,unit=entry.unit or p.unit,comment=entry.comment,status='К заказу'))
        checklist.submitted_order_id=order.id
    checklist.completed_at=datetime.now(timezone.utc)
    db.commit();db.refresh(checklist)
    if order: db.refresh(order)
    return {'checklist':checklist_json(checklist),'order':order_json(order) if order else None}

@app.get('/api/suppliers')
def suppliers(db: Session=Depends(get_db), u: User=Depends(current_user)):
    return [{'id':s.id,'name':s.name,'active':s.active} for s in db.query(Supplier).order_by(Supplier.name).all()]

@app.post('/api/suppliers')
def create_supplier(body: SupplierIn, db: Session=Depends(get_db), u: User=Depends(require_role('chef'))):
    name=body.name.strip()
    if not name: raise HTTPException(400,'Укажите название поставщика')
    if db.query(Supplier).filter(Supplier.name==name).first(): raise HTTPException(409,'Поставщик уже существует')
    s=Supplier(name=name,active=body.active); db.add(s); db.commit(); db.refresh(s); return {'id':s.id,'name':s.name,'active':s.active}

@app.get('/api/users')
def users(db: Session=Depends(get_db), u: User=Depends(require_role('chef'))): return [user_json(x) for x in db.query(User).order_by(User.name).all()]

@app.post('/api/users')
def create_user(body: UserIn, db: Session=Depends(get_db), u: User=Depends(require_role('chef'))):
    if body.role not in ('chef','souschef','provisioner'): raise HTTPException(400,'Недопустимая роль')
    if db.query(User).filter(User.login==body.login.strip()).first(): raise HTTPException(409,'Логин уже используется')
    x=User(name=body.name.strip(),login=body.login.strip(),password_hash=hasher.hash(body.password),role=body.role,active=True,must_change_password=True); db.add(x); db.commit(); db.refresh(x); return user_json(x)

@app.put('/api/users/{user_id}')
def update_user(user_id: int, body: UserUpdate, db: Session=Depends(get_db), u: User=Depends(require_role('chef'))):
    x=db.get(User,user_id)
    if not x: raise HTTPException(404,'Пользователь не найден')
    if x.id==u.id and not body.active: raise HTTPException(400,'Нельзя отключить свой аккаунт')
    if body.role not in ('chef','souschef','provisioner'): raise HTTPException(400,'Недопустимая роль')
    x.name=body.name.strip(); x.role=body.role; x.active=body.active
    if body.password:
        x.password_hash=hasher.hash(body.password); x.must_change_password=True
    db.commit(); db.refresh(x); return user_json(x)

@app.delete('/api/users/{user_id}')
def delete_user(user_id: int, db: Session=Depends(get_db), u: User=Depends(require_role('chef'))):
    x=db.get(User,user_id)
    if not x: raise HTTPException(404,'Пользователь не найден')
    if x.id==u.id: raise HTTPException(400,'Нельзя удалить свой аккаунт')
    x.active=False
    db.commit()
    return {'ok':True,'user_id':x.id,'active':False}

@app.post('/api/orders')
def create_order(body: OrderIn, db: Session=Depends(get_db), u: User=Depends(require_role('souschef','chef'))):
    if len(body.items)>200: raise HTTPException(400,'В одной заявке может быть не более 200 позиций')
    validate_planned_date(body.planned_date)
    order=Order(author_id=u.id,planned_date=body.planned_date,status='Отправлена',is_approved=False,comment=body.comment)
    db.add(order); db.flush()
    for entry in body.items:
        p=db.get(Product,entry.product_id)
        if not p or not p.active: raise HTTPException(400,'Один из товаров больше недоступен')
        order.items.append(OrderItem(product_id=p.id,product_name=p.name,category=p.category,supplier_name=p.supplier.name if p.supplier else (p.source_supplier_label or None),quantity=entry.quantity,unit=(entry.unit.strip() if entry.unit and entry.unit.strip() else p.unit),comment=entry.comment,status='К заказу'))
    db.commit(); db.refresh(order); return order_json(order)

@app.put('/api/orders/{order_id}')
def update_order(order_id: int, body: OrderUpdate, db: Session=Depends(get_db), u: User=Depends(require_role('souschef','chef'))):
    if len(body.items)>200: raise HTTPException(400,'В одной заявке может быть не более 200 позиций')
    order=db.get(Order,order_id)
    if not order: raise HTTPException(404,'Заявка не найдена')
    if u.role=='souschef' and order.author_id!=u.id: raise HTTPException(403,'Можно редактировать только свои заявки')
    if order.status=='Отменена': raise HTTPException(400,'Отменённую заявку нельзя изменить')
    if any(item.status!='К заказу' for item in order.items):
        raise HTTPException(400,'Закупщик уже начал обработку. Изменения этой заявки заблокированы')
    validate_planned_date(body.planned_date, order_kitchen_date(order))
    order.items.clear()
    db.flush()
    for entry in body.items:
        p=db.get(Product,entry.product_id)
        if not p or not p.active: raise HTTPException(400,'Один из товаров больше недоступен')
        order.items.append(OrderItem(product_id=p.id,product_name=p.name,category=p.category,supplier_name=p.supplier.name if p.supplier else (p.source_supplier_label or None),quantity=entry.quantity,unit=(entry.unit.strip() if entry.unit and entry.unit.strip() else p.unit),comment=entry.comment,status='К заказу'))
    order.comment=body.comment
    order.planned_date=body.planned_date
    order.status='Отправлена'
    order.is_approved=False
    order.requires_revision=False
    order.review_note=None
    db.commit(); db.refresh(order); return order_json(order)

@app.delete('/api/orders/{order_id}')
def delete_order(order_id: int, db: Session=Depends(get_db), u: User=Depends(require_role('souschef','chef'))):
    order=db.get(Order,order_id)
    if not order: raise HTTPException(404,'Заявка не найдена')
    if u.role=='souschef' and order.author_id!=u.id: raise HTTPException(403,'Можно удалить только свои заявки')
    if any(item.status!='К заказу' for item in order.items):
        raise HTTPException(400,'Закупщик уже начал обработку. Удаление заявки заблокировано')
    # Unlock a completed daily checklist so its author can revise and submit it again.
    linked_checklists=db.query(DailyChecklist).filter_by(submitted_order_id=order.id).all()
    for checklist in linked_checklists:
        checklist.submitted_order_id=None
        checklist.completed_at=None
    db.flush()
    db.delete(order)
    db.commit()
    return {'ok':True,'order_id':order_id}

@app.patch('/api/orders/{order_id}/approval')
def update_order_approval(order_id: int, body: OrderApprovalIn, db: Session=Depends(get_db), u: User=Depends(require_role('chef'))):
    order=db.get(Order,order_id)
    if not order: raise HTTPException(404,'Заявка не найдена')
    if body.is_approved and order.author_id==u.id: raise HTTPException(403,'Нельзя согласовать собственную заявку')
    if order.status=='Отменена': raise HTTPException(400,'Отменённую заявку нельзя согласовать')
    if not body.is_approved and any(item.status!='К заказу' for item in order.items):
        raise HTTPException(400,'Закупщик уже начал обработку. Согласование нельзя снять')
    order.is_approved=body.is_approved
    if body.is_approved:
        order.requires_revision=False
        order.review_note=None
    db.commit(); db.refresh(order); return order_json(order)

@app.patch('/api/orders/{order_id}/return')
def return_order_for_revision(order_id: int, body: OrderReturnIn, db: Session=Depends(get_db), u: User=Depends(require_role('chef'))):
    order=db.get(Order,order_id)
    if not order: raise HTTPException(404,'Заявка не найдена')
    if order.author_id==u.id: raise HTTPException(403,'Нельзя вернуть собственную заявку')
    if order.status=='Отменена': raise HTTPException(400,'Отменённую заявку нельзя вернуть')
    if any(item.status!='К заказу' for item in order.items): raise HTTPException(400,'Закупщик уже начал обработку заявки')
    order.is_approved=False
    order.requires_revision=True
    order.review_note=body.comment.strip()
    db.commit();db.refresh(order)
    return order_json(order)

@app.get('/api/orders')
def list_orders(status: str='', date_from: Optional[str]=None,date_to: Optional[str]=None, db: Session=Depends(get_db), u: User=Depends(current_user)):
    q=db.query(Order)
    if u.role=='souschef': q=q.filter(Order.author_id==u.id)
    if u.role=='provisioner': q=q.filter(Order.is_approved.is_(True))
    if status: q=q.filter(Order.status==status)
    if date_from:
        try:
            start = date.fromisoformat(date_from)
            start_at = datetime.combine(start, datetime.min.time(), tzinfo=KITCHEN_TZ).astimezone(timezone.utc)
            q=q.filter(or_(Order.planned_date>=start, and_(Order.planned_date.is_(None), Order.created_at>=start_at)))
        except ValueError: raise HTTPException(400,'Неверная начальная дата')
    if date_to:
        try:
            end = date.fromisoformat(date_to)
            end_at = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=KITCHEN_TZ).astimezone(timezone.utc)
            q=q.filter(or_(Order.planned_date<=end, and_(Order.planned_date.is_(None), Order.created_at<end_at)))
        except ValueError: raise HTTPException(400,'Неверная конечная дата')
    return [order_json(o) for o in q.order_by(Order.created_at.desc()).all()]

@app.get('/api/supplier-orders.xlsx')
def export_supplier_orders(supplier_name: str=Query(min_length=1), planned_date: Optional[date]=None, db: Session=Depends(get_db), u: User=Depends(require_role('provisioner','chef'))):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    orders=db.query(Order).filter(Order.is_approved.is_(True),Order.status!='Отменена').order_by(Order.planned_date,Order.id).all()
    rows=[]
    for order in orders:
        day=order_kitchen_date(order)
        if planned_date and day!=planned_date: continue
        for item in order.items:
            if (item.supplier_name or 'Без поставщика')==supplier_name:
                rows.append((day,order.id,order.author.name,item.category,item.product_name,item.quantity,item.unit or '',item.comment or '',item.status))
    if not rows: raise HTTPException(404,'Для этого поставщика нет согласованных позиций на выбранную дату')
    workbook=Workbook();sheet=workbook.active;sheet.title='Заказ поставщику'
    sheet.append(['Дата закупки','Заявка','Су-шеф','Категория','Товар','Количество','Единица','Комментарий','Статус'])
    for cell in sheet[1]:
        cell.font=Font(bold=True,color='FFFFFF');cell.fill=PatternFill('solid',fgColor='176849');cell.alignment=Alignment(vertical='center')
    for row in rows: sheet.append([row[0].strftime('%d.%m.%Y'),*row[1:]])
    sheet.freeze_panes='A2';sheet.auto_filter.ref=sheet.dimensions
    for col,width in {'A':16,'B':12,'C':22,'D':28,'E':48,'F':16,'G':14,'H':38,'I':18}.items():sheet.column_dimensions[col].width=width
    for row in sheet.iter_rows(min_row=2):
        for cell in row:cell.alignment=Alignment(vertical='top',wrap_text=True)
    content=BytesIO();workbook.save(content);content.seek(0)
    slug=re.sub(r'[^\w.-]+','_',supplier_name,flags=re.UNICODE).strip('_') or 'supplier'
    suffix=f'_{planned_date.isoformat()}' if planned_date else ''
    filename=f'Заказ_{slug}{suffix}.xlsx'
    return StreamingResponse(content,media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':f"attachment; filename*=UTF-8''{quote(filename)}"})

@app.patch('/api/order-items/{item_id}/status')
def update_item_status(item_id: int, body: ItemStatusIn, db: Session=Depends(get_db), u: User=Depends(require_role('provisioner','chef'))):
    if body.status not in ('К заказу','Заказано','Получено','Нужно уточнить'): raise HTTPException(400,'Недопустимый статус позиции')
    item=db.get(OrderItem,item_id)
    if not item: raise HTTPException(404,'Позиция не найдена')
    order=db.get(Order,item.order_id)
    if not order.is_approved: raise HTTPException(400,'Заявка ещё не согласована шеф-поваром')
    item.status=body.status
    statuses={i.status for i in order.items}
    order.status='Выполнена' if statuses=={'Получено'} else 'В работе'
    db.commit(); return {'ok':True,'status':item.status,'order_status':order.status}

@app.patch('/api/orders/{order_id}/cancel')
def cancel_order(order_id: int, db: Session=Depends(get_db), u: User=Depends(require_role('chef'))):
    order=db.get(Order,order_id)
    if not order: raise HTTPException(404,'Заявка не найдена')
    order.status='Отменена'; db.commit(); return order_json(order)

# The Railway image includes the built React app and serves it from the same
# origin as the API. Local Docker Compose continues to serve the UI via Nginx.
FRONTEND_DIR = Path(__file__).resolve().parents[1] / 'frontend'
ASSETS_DIR = FRONTEND_DIR / 'assets'
if ASSETS_DIR.is_dir():
    app.mount('/assets', StaticFiles(directory=ASSETS_DIR), name='assets')

    @app.get('/{path:path}', include_in_schema=False)
    def frontend(path: str):
        requested = (FRONTEND_DIR / path).resolve()
        if requested.is_relative_to(FRONTEND_DIR.resolve()) and requested.is_file():
            return FileResponse(requested)
        return FileResponse(FRONTEND_DIR / 'index.html')
