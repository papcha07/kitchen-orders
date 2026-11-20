from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import json, os, secrets
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Depends, HTTPException, Response, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from jose import jwt, JWTError
from pwdlib import PasswordHash
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, and_, create_engine, or_
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
    supplier: Mapped[Optional[Supplier]]=relationship()

class Order(Base):
    __tablename__='orders'
    id: Mapped[int]=mapped_column(primary_key=True)
    author_id: Mapped[int]=mapped_column(ForeignKey('users.id'), index=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    planned_date: Mapped[Optional[date]]=mapped_column(Date, nullable=True, index=True)
    status: Mapped[str]=mapped_column(String(30), default='Отправлена', index=True)
    is_approved: Mapped[bool]=mapped_column(Boolean, default=False, nullable=False, index=True)
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

class LoginIn(BaseModel): login: str; password: str
class UserIn(BaseModel): name: str; login: str; password: str = Field(min_length=8); role: str
class UserUpdate(BaseModel): name: str; role: str; active: bool=True; password: Optional[str]=None
class PasswordChangeIn(BaseModel): current_password: str; new_password: str = Field(min_length=8)
class ProductIn(BaseModel): name: str; category: str; unit: Optional[str]=None; min_stock: Optional[float]=None; supplier_id: Optional[int]=None; active: bool=True
class SupplierIn(BaseModel): name: str; active: bool=True
class ItemIn(BaseModel): product_id: int; quantity: float=Field(gt=0); comment: Optional[str]=None
class OrderIn(BaseModel): items: list[ItemIn]=Field(min_length=1); planned_date: date=Field(default_factory=kitchen_today); comment: Optional[str]=None
class ItemStatusIn(BaseModel): status: str
class OrderUpdate(BaseModel): items: list[ItemIn]=Field(min_length=1); planned_date: date; comment: Optional[str]=None
class OrderApprovalIn(BaseModel): is_approved: bool

app=FastAPI(title='Kitchen Orders API', version='1.0.0')
app.add_middleware(CORSMiddleware, allow_origins=os.getenv('CORS_ORIGINS','http://localhost:8080').split(','), allow_credentials=True, allow_methods=['*'], allow_headers=['*'])

def get_db():
    db=SessionLocal()
    try: yield db
    finally: db.close()

def user_json(u: User): return {'id':u.id,'name':u.name,'login':u.login,'role':u.role,'active':u.active,'must_change_password':u.must_change_password,'created_at':u.created_at.isoformat() if u.created_at else None}
def product_json(p: Product): return {'id':p.id,'name':p.name,'category':p.category,'unit':p.unit,'min_stock':p.min_stock,'supplier_id':p.supplier_id,'supplier_name':p.supplier.name if p.supplier else None,'source_supplier_label':p.source_supplier_label,'needs_supplier':p.needs_supplier,'active':p.active}
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
    return {'id':o.id,'author_id':o.author_id,'author_name':o.author.name,'created_at':o.created_at.isoformat(),'planned_date':order_kitchen_date(o).isoformat(),'status':o.status,'is_approved':o.is_approved,'comment':o.comment,'items':[{'id':i.id,'product_id':i.product_id,'name':i.product_name,'category':i.category,'supplier_name':i.supplier_name,'quantity':i.quantity,'unit':i.unit,'comment':i.comment,'status':i.status} for i in o.items]}

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
        if not db.query(Product).first():
            seed_path=Path(__file__).resolve().parents[1]/'seed.json'
            if seed_path.exists():
                data=json.loads(seed_path.read_text(encoding='utf-8'))
                supplier_by_name={}
                for name in data.get('suppliers',[]):
                    s=Supplier(name=name); db.add(s); supplier_by_name[name]=s
                db.flush()
                for raw in data.get('products',[]):
                    label=raw.get('supplier_name'); mapped=bool(label and label in supplier_by_name and not raw.get('needs_supplier'))
                    db.add(Product(name=raw['name'],category=raw['category'],unit=raw.get('unit'),min_stock=raw.get('min_stock'),supplier_id=supplier_by_name[label].id if mapped else None,source_supplier_label=label,needs_supplier=not mapped,source_sheet=raw.get('source')))
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
    p.needs_supplier=body.supplier_id is None; p.source_supplier_label=None
    db.commit(); db.refresh(p); return product_json(p)

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

@app.post('/api/orders')
def create_order(body: OrderIn, db: Session=Depends(get_db), u: User=Depends(require_role('souschef','chef'))):
    if len(body.items)>200: raise HTTPException(400,'В одной заявке может быть не более 200 позиций')
    validate_planned_date(body.planned_date)
    order=Order(author_id=u.id,planned_date=body.planned_date,status='Отправлена',is_approved=False,comment=body.comment)
    db.add(order); db.flush()
    for entry in body.items:
        p=db.get(Product,entry.product_id)
        if not p or not p.active: raise HTTPException(400,'Один из товаров больше недоступен')
        order.items.append(OrderItem(product_id=p.id,product_name=p.name,category=p.category,supplier_name=p.supplier.name if p.supplier else (p.source_supplier_label or None),quantity=entry.quantity,unit=p.unit,comment=entry.comment,status='К заказу'))
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
        order.items.append(OrderItem(product_id=p.id,product_name=p.name,category=p.category,supplier_name=p.supplier.name if p.supplier else (p.source_supplier_label or None),quantity=entry.quantity,unit=p.unit,comment=entry.comment,status='К заказу'))
    order.comment=body.comment
    order.planned_date=body.planned_date
    order.status='Отправлена'
    order.is_approved=False
    db.commit(); db.refresh(order); return order_json(order)

@app.patch('/api/orders/{order_id}/approval')
def update_order_approval(order_id: int, body: OrderApprovalIn, db: Session=Depends(get_db), u: User=Depends(require_role('chef'))):
    order=db.get(Order,order_id)
    if not order: raise HTTPException(404,'Заявка не найдена')
    if order.status=='Отменена': raise HTTPException(400,'Отменённую заявку нельзя согласовать')
    if not body.is_approved and any(item.status!='К заказу' for item in order.items):
        raise HTTPException(400,'Закупщик уже начал обработку. Согласование нельзя снять')
    order.is_approved=body.is_approved
    db.commit(); db.refresh(order); return order_json(order)

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
