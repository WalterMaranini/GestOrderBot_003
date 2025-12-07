# rest_api.py
#
# API REST locale per:
# - Clienti
# - Articoli
# - Prezzi
# - Giacenze
# - Ordini (testata + righe)
#
# Pensata per essere chiamata dal tuo MCP server.
#
# Avvio:
#   uvicorn rest_api:app --host 127.0.0.1 --port 8001 --reload

from datetime import date, datetime
from typing import List, Optional
import logging

from fastapi import FastAPI, HTTPException, Query, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from db_models import (
    init_db,
    get_db,
    Customer,
    Article,
    OrderHeader,
    OrderLine,
    Price,
    StockLevel,
)

# ===================== LOGGING =====================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
logger = logging.getLogger("orders_rest_api")

app = FastAPI(
    title="Orders REST API (locale)",
    description="API per gestione ordini, clienti, articoli, prezzi e giacenze.",
    version="1.0.0",
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """
    Middleware che logga:
    - metodo, URL, client
    - body della richiesta (se testuale)
    - status code e body della risposta (se testuale)

    Il body viene troncato se troppo lungo.
    """
    method = request.method
    url = str(request.url)
    client_host = request.client.host if request.client else "unknown"

    # --- Body della richiesta ---
    try:
        body_bytes = await request.body()
        if body_bytes:
            try:
                body_text = body_bytes.decode("utf-8")
            except UnicodeDecodeError:
                body_text = repr(body_bytes[:500])
        else:
            body_text = ""

        if len(body_text) > 1000:
            body_log = body_text[:1000] + "... [troncato]"
        else:
            body_log = body_text
    except Exception as e:
        body_log = f"<errore lettura body: {e}>"

    logger.info(
        "REST IN  <- %s %s (client=%s) body=%s",
        method,
        url,
        client_host,
        body_log,
    )

    # --- Chiamata all'handler ---
    try:
        response = await call_next(request)
    except Exception as exc:
        logger.exception("Errore durante la gestione di %s %s: %s", method, url, exc)
        raise

    # --- Body della risposta ---
    try:
        resp_body_bytes = b""
        async for chunk in response.body_iterator:
            resp_body_bytes += chunk

        new_response = Response(
            content=resp_body_bytes,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
        )

        if resp_body_bytes:
            try:
                resp_text = resp_body_bytes.decode("utf-8")
            except UnicodeDecodeError:
                resp_text = repr(resp_body_bytes[:500])
        else:
            resp_text = ""

        if len(resp_text) > 1000:
            resp_log = resp_text[:1000] + "... [troncato]"
        else:
            resp_log = resp_text

        logger.info(
            "REST OUT -> %s %s (status=%s) body=%s",
            method,
            url,
            new_response.status_code,
            resp_log,
        )

        return new_response

    except Exception as e:
        logger.warning(
            "Impossibile leggere il body della risposta per %s %s: %s",
            method,
            url,
            e,
        )
        logger.info(
            "REST OUT -> %s %s (status=%s)",
            method,
            url,
            response.status_code,
        )
        return response


@app.on_event("startup")
def on_startup():
    # Crea tabelle se non esistono
    init_db()


# ===================== MODELLI Pydantic =====================

# ---- Clienti ----

class CustomerIn(BaseModel):
    code: str = Field(..., description="Codice cliente")
    name: str = Field(..., description="Ragione sociale")
    address: Optional[str] = None
    city: Optional[str] = None
    province: Optional[str] = None
    country: Optional[str] = "IT"


class CustomerOut(BaseModel):
    id: int
    code: str
    name: str
    address: Optional[str]
    city: Optional[str]
    province: Optional[str]
    country: Optional[str]


# ---- Articoli ----

class ArticleIn(BaseModel):
    code: str
    description: str
    unit: str = "PZ"


class ArticleOut(BaseModel):
    id: int
    code: str
    description: str
    unit: str


# ---- Prezzi ----

class PriceIn(BaseModel):
    customer_code: Optional[str] = Field(
        None,
        description="Codice cliente, opzionale (se None = listino generico)",
    )
    article_code: str
    price: float
    currency: str = "EUR"


class PriceItem(BaseModel):
    customer_code: Optional[str]
    article_code: str
    price: float
    currency: str = "EUR"


# ---- Giacenze ----

class StockIn(BaseModel):
    article_code: str
    warehouse_code: str = "MAIN"
    quantity: float


class StockItem(BaseModel):
    article_code: str
    warehouse_code: str
    quantity: float


# ---- Ordini ----

class OrderLineIn(BaseModel):
    article_code: str = Field(..., description="Codice articolo")
    quantity: float = Field(..., gt=0, description="Quantità ordinata")


class OrderCreate(BaseModel):
    customer_code: str = Field(..., description="Codice cliente")
    delivery_date: str = Field(..., description="Data consegna richiesta (YYYY-MM-DD)")
    lines: List[OrderLineIn] = Field(..., description="Righe ordine")


class OrderLineOut(BaseModel):
    line_no: int
    article_code: str
    quantity: float
    unit_price: Optional[float] = None
    discount: Optional[float] = None


class OrderOut(BaseModel):
    order_id: int
    customer_code: str
    order_date: str
    delivery_date: str
    status: str
    lines: List[OrderLineOut]


# ===================== ENDPOINT CLIENTI =====================

@app.post("/customers", response_model=CustomerOut)
def create_customer(payload: CustomerIn, db: Session = Depends(get_db)):
    existing = db.query(Customer).filter(Customer.code == payload.code).first()
    if existing:
        raise HTTPException(status_code=400, detail="Cliente già esistente")

    customer = Customer(
        code=payload.code,
        name=payload.name,
        address=payload.address,
        city=payload.city,
        province=payload.province,
        country=payload.country,
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)

    return CustomerOut(
        id=customer.id,
        code=customer.code,
        name=customer.name,
        address=customer.address,
        city=customer.city,
        province=customer.province,
        country=customer.country,
    )


@app.get("/customers", response_model=List[CustomerOut])
def list_customers(
    code: Optional[str] = Query(None, description="Filtra per codice (LIKE)"),
    name: Optional[str] = Query(None, description="Filtra per nome (LIKE)"),
    db: Session = Depends(get_db),
):
    """
    Lista / ricerca clienti.

    - senza parametri -> tutti i clienti
    - code -> filtro su codice (LIKE)
    - name -> filtro su nome (LIKE)
    """
    q = db.query(Customer)

    if code:
        q = q.filter(Customer.code.ilike(f"%{code}%"))
    if name:
        q = q.filter(Customer.name.ilike(f"%{name}%"))

    rows = q.order_by(Customer.code).all()
    return [
        CustomerOut(
            id=r.id,
            code=r.code,
            name=r.name,
            address=r.address,
            city=r.city,
            province=r.province,
            country=r.country,
        )
        for r in rows
    ]


@app.get("/customers/{code}", response_model=CustomerOut)
def get_customer(code: str, db: Session = Depends(get_db)):
    """
    Dettaglio cliente per codice.
    """
    cust = db.query(Customer).filter(Customer.code == code).first()
    if not cust:
        raise HTTPException(status_code=404, detail="Cliente non trovato")

    return CustomerOut(
        id=cust.id,
        code=cust.code,
        name=cust.name,
        address=cust.address,
        city=cust.city,
        province=cust.province,
        country=cust.country,
    )


@app.delete("/customers/{code}", status_code=204)
def delete_customer(code: str, db: Session = Depends(get_db)):
    """
    Cancellazione cliente per codice.
    Impedita se esistono ordini collegati.
    """
    cust = db.query(Customer).filter(Customer.code == code).first()
    if not cust:
        raise HTTPException(status_code=404, detail="Cliente non trovato")

    if cust.orders and len(cust.orders) > 0:
        raise HTTPException(
            status_code=409,
            detail="Impossibile cancellare: esistono ordini collegati al cliente",
        )

    db.delete(cust)
    db.commit()
    return Response(status_code=204)


# ===================== ENDPOINT ARTICOLI =====================

@app.post("/articles", response_model=ArticleOut)
def create_article(payload: ArticleIn, db: Session = Depends(get_db)):
    existing = db.query(Article).filter(Article.code == payload.code).first()
    if existing:
        raise HTTPException(status_code=400, detail="Articolo già esistente")

    art = Article(
        code=payload.code,
        description=payload.description,
        unit=payload.unit,
    )
    db.add(art)
    db.commit()
    db.refresh(art)

    return ArticleOut(
        id=art.id,
        code=art.code,
        description=art.description,
        unit=art.unit,
    )


@app.get("/articles", response_model=List[ArticleOut])
def list_articles(
    code: Optional[str] = Query(None, description="Filtra per codice (LIKE)"),
    description: Optional[str] = Query(None, description="Filtra per descrizione (LIKE)"),
    db: Session = Depends(get_db),
):
    """
    Lista / ricerca articoli.

    - senza parametri -> tutti gli articoli
    - code -> filtro su codice (LIKE)
    - description -> filtro su descrizione (LIKE)
    """
    q = db.query(Article)

    if code:
        q = q.filter(Article.code.ilike(f"%{code}%"))
    if description:
        q = q.filter(Article.description.ilike(f"%{description}%"))

    rows = q.order_by(Article.code).all()
    return [
        ArticleOut(
            id=r.id,
            code=r.code,
            description=r.description,
            unit=r.unit,
        )
        for r in rows
    ]


@app.get("/articles/{code}", response_model=ArticleOut)
def get_article(code: str, db: Session = Depends(get_db)):
    """
    Dettaglio articolo per codice.
    """
    art = db.query(Article).filter(Article.code == code).first()
    if not art:
        raise HTTPException(status_code=404, detail="Articolo non trovato")

    return ArticleOut(
        id=art.id,
        code=art.code,
        description=art.description,
        unit=art.unit,
    )


@app.delete("/articles/{code}", status_code=204)
def delete_article(code: str, db: Session = Depends(get_db)):
    """
    Cancellazione articolo per codice.
    Impedita se esistono:
    - righe ordine
    - prezzi
    - giacenze
    """
    art = db.query(Article).filter(Article.code == code).first()
    if not art:
        raise HTTPException(status_code=404, detail="Articolo non trovato")

    if art.order_lines or art.prices or art.stock_levels:
        raise HTTPException(
            status_code=409,
            detail="Impossibile cancellare: esistono dati collegati (ordini, prezzi o giacenze)",
        )

    db.delete(art)
    db.commit()
    return Response(status_code=204)


# ===================== ENDPOINT PREZZI =====================

@app.post("/prices", response_model=PriceItem)
def create_price(payload: PriceIn, db: Session = Depends(get_db)):
    # Cliente (opzionale)
    customer_id = None
    customer_code_out: Optional[str] = None
    if payload.customer_code:
        cust = db.query(Customer).filter(Customer.code == payload.customer_code).first()
        if not cust:
            raise HTTPException(status_code=400, detail="Cliente non trovato")
        customer_id = cust.id
        customer_code_out = cust.code

    # Articolo obbligatorio
    art = db.query(Article).filter(Article.code == payload.article_code).first()
    if not art:
        raise HTTPException(status_code=400, detail="Articolo non trovato")

    # Controlla se già esiste
    existing = (
        db.query(Price)
        .filter(Price.customer_id == customer_id, Price.article_id == art.id)
        .first()
    )
    if existing:
        existing.price = payload.price
        existing.currency = payload.currency
        db.commit()
        p = existing
    else:
        p = Price(
            customer_id=customer_id,
            article_id=art.id,
            price=payload.price,
            currency=payload.currency,
        )
        db.add(p)
        db.commit()
        db.refresh(p)

    return PriceItem(
        customer_code=customer_code_out,
        article_code=art.code,
        price=p.price,
        currency=p.currency,
    )


@app.get("/prices", response_model=List[PriceItem])
def get_price_list(
    customer_code: Optional[str] = Query(None),
    article_code: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """
    Restituisce il prezzo di un articolo (o una lista di prezzi) con la logica:

    - Se è specificato article_code:
        * Se è specificato anche customer_code:
            1) prova il prezzo specifico cliente+articolo
            2) se NON esiste, usa il prezzo generico dell'articolo (customer_id NULL)
        * Se NON è specificato customer_code:
            - restituisce il prezzo generico per l'articolo (se esiste)

    - Se NON è specificato article_code:
        * Se customer_code è valorizzato:
            1) prova tutti i prezzi specifici del cliente
            2) se NON ne esistono, restituisce tutti i prezzi generici
        * Se customer_code è NULL:
            - restituisce tutti i prezzi generici
    """

    # ----- CASO 1: article_code specificato -----
    if article_code:
        # Verifica articolo
        art = db.query(Article).filter(Article.code == article_code).first()
        if not art:
            raise HTTPException(status_code=400, detail="Articolo non trovato")

        # 1A) cliente + articolo
        if customer_code:
            cust = db.query(Customer).filter(Customer.code == customer_code).first()
            if not cust:
                raise HTTPException(status_code=400, detail="Cliente non trovato")

            # prezzo specifico cliente+articolo
            specific = (
                db.query(Price)
                .filter(
                    Price.customer_id == cust.id,
                    Price.article_id == art.id,
                )
                .first()
            )
            if specific:
                return [
                    PriceItem(
                        customer_code=cust.code,
                        article_code=art.code,
                        price=specific.price,
                        currency=specific.currency,
                    )
                ]

            # fallback generico
            generic = (
                db.query(Price)
                .filter(
                    Price.customer_id.is_(None),
                    Price.article_id == art.id,
                )
                .first()
            )
            if generic:
                return [
                    PriceItem(
                        customer_code=None,
                        article_code=art.code,
                        price=generic.price,
                        currency=generic.currency,
                    )
                ]

            # niente prezzi
            return []

        # 1B) solo article_code -> prezzo generico
        generic = (
            db.query(Price)
            .filter(
                Price.customer_id.is_(None),
                Price.article_id == art.id,
            )
            .first()
        )
        if generic:
            return [
                PriceItem(
                    customer_code=None,
                    article_code=art.code,
                    price=generic.price,
                    currency=generic.currency,
                )
            ]
        return []

    # ----- CASO 2: nessun article_code -----

    # 2A) Abbiamo customer_code -> specifici o fallback generici
    if customer_code:
        cust = db.query(Customer).filter(Customer.code == customer_code).first()
        if not cust:
            raise HTTPException(status_code=400, detail="Cliente non trovato")

        # prima prezzi specifici del cliente
        specific_prices = (
            db.query(Price)
            .join(Article)
            .filter(Price.customer_id == cust.id)
            .all()
        )

        if specific_prices:
            return [
                PriceItem(
                    customer_code=customer_code,
                    article_code=p.article.code,
                    price=p.price,
                    currency=p.currency,
                )
                for p in specific_prices
            ]

        # fallback: TUTTI i prezzi generici
        generic_prices = (
            db.query(Price)
            .join(Article)
            .filter(Price.customer_id.is_(None))
            .all()
        )

        return [
            PriceItem(
                customer_code=None,
                article_code=p.article.code,
                price=p.price,
                currency=p.currency,
            )
            for p in generic_prices
        ]

    # 2B) Né cliente né articolo -> tutti i generici
    generic_prices = (
        db.query(Price)
        .join(Article)
        .filter(Price.customer_id.is_(None))
        .all()
    )

    return [
        PriceItem(
            customer_code=None,
            article_code=p.article.code,
            price=p.price,
            currency=p.currency,
        )
        for p in generic_prices
    ]


@app.delete("/prices", status_code=204)
def delete_price(
    article_code: str = Query(..., description="Codice articolo"),
    customer_code: Optional[str] = Query(
        None, description="Codice cliente (se omesso cancella il prezzo generico)"
    ),
    db: Session = Depends(get_db),
):
    """
    Cancellazione prezzo:

    - se customer_code è valorizzato -> cancella prezzo cliente+articolo
    - altrimenti -> cancella prezzo generico per articolo
    """
    art = db.query(Article).filter(Article.code == article_code).first()
    if not art:
        raise HTTPException(status_code=404, detail="Articolo non trovato")

    if customer_code:
        cust = db.query(Customer).filter(Customer.code == customer_code).first()
        if not cust:
            raise HTTPException(status_code=404, detail="Cliente non trovato")
        price = (
            db.query(Price)
            .filter(Price.customer_id == cust.id, Price.article_id == art.id)
            .first()
        )
    else:
        price = (
            db.query(Price)
            .filter(Price.customer_id.is_(None), Price.article_id == art.id)
            .first()
        )

    if not price:
        raise HTTPException(status_code=404, detail="Prezzo non trovato")

    db.delete(price)
    db.commit()
    return Response(status_code=204)


# ===================== ENDPOINT GIACENZE =====================

@app.post("/stock", response_model=StockItem)
def create_or_update_stock(payload: StockIn, db: Session = Depends(get_db)):
    art = db.query(Article).filter(Article.code == payload.article_code).first()
    if not art:
        raise HTTPException(status_code=400, detail="Articolo non trovato")

    existing = (
        db.query(StockLevel)
        .filter(
            StockLevel.article_id == art.id,
            StockLevel.warehouse_code == payload.warehouse_code,
        )
        .first()
    )

    if existing:
        existing.quantity = payload.quantity
        db.commit()
        s = existing
    else:
        s = StockLevel(
            article_id=art.id,
            warehouse_code=payload.warehouse_code,
            quantity=payload.quantity,
        )
        db.add(s)
        db.commit()
        db.refresh(s)

    return StockItem(
        article_code=art.code,
        warehouse_code=s.warehouse_code,
        quantity=s.quantity,
    )


@app.get("/stock", response_model=List[StockItem])
def get_stock(
    article_code: Optional[str] = Query(None),
    warehouse_code: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """
    Lista / ricerca giacenze con filtri per articolo e magazzino.
    """
    q = db.query(StockLevel).join(Article)

    if article_code:
        q = q.filter(Article.code == article_code)
    if warehouse_code:
        q = q.filter(StockLevel.warehouse_code == warehouse_code)

    rows = q.all()
    out: List[StockItem] = []
    for r in rows:
        out.append(
            StockItem(
                article_code=r.article.code,
                warehouse_code=r.warehouse_code,
                quantity=r.quantity,
            )
        )
    return out


@app.delete("/stock", status_code=204)
def delete_stock(
    article_code: str = Query(..., description="Codice articolo"),
    warehouse_code: str = Query(..., description="Codice magazzino"),
    db: Session = Depends(get_db),
):
    """
    Cancellazione riga giacenza per articolo+magazzino.
    """
    art = db.query(Article).filter(Article.code == article_code).first()
    if not art:
        raise HTTPException(status_code=404, detail="Articolo non trovato")

    stock = (
        db.query(StockLevel)
        .filter(
            StockLevel.article_id == art.id,
            StockLevel.warehouse_code == warehouse_code,
        )
        .first()
    )
    if not stock:
        raise HTTPException(status_code=404, detail="Giacenza non trovata")

    db.delete(stock)
    db.commit()
    return Response(status_code=204)


# ===================== ENDPOINT ORDINI =====================

@app.post("/orders", response_model=OrderOut)
def create_order(payload: OrderCreate, db: Session = Depends(get_db)):
    """
    Crea un nuovo ordine nel DB.

    Mappato da:
      Service name="create_order" method="POST" path="/orders"
      body: { customer_code, delivery_date, lines }
    """

    # Cliente
    customer = db.query(Customer).filter(Customer.code == payload.customer_code).first()
    if not customer:
        raise HTTPException(
            status_code=400,
            detail=f"Cliente con codice {payload.customer_code} non trovato",
        )

    if not payload.lines:
        raise HTTPException(status_code=400, detail="Nessuna riga ordine fornita")

    # Parse delivery_date
    try:
        delivery_date = date.fromisoformat(payload.delivery_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Formato delivery_date non valido, usare YYYY-MM-DD")

    order_header = OrderHeader(
        customer_id=customer.id,
        order_date=date.today(),
        delivery_date=delivery_date,
        status="INSERTED",
        created_at=datetime.utcnow(),
    )
    db.add(order_header)
    db.flush()  # per avere order_header.id

    lines_out: List[OrderLineOut] = []

    for idx, line_in in enumerate(payload.lines, start=1):
        art = db.query(Article).filter(Article.code == line_in.article_code).first()
        if not art:
            raise HTTPException(
                status_code=400,
                detail=f"Articolo con codice {line_in.article_code} non trovato",
            )

        # Qui potresti recuperare il prezzo da tabella prices, se vuoi.
        unit_price = None
        discount = None

        line = OrderLine(
            order_id=order_header.id,
            line_no=idx,
            article_id=art.id,
            quantity=line_in.quantity,
            unit_price=unit_price,
            discount=discount,
        )
        db.add(line)

        lines_out.append(
            OrderLineOut(
                line_no=idx,
                article_code=art.code,
                quantity=line_in.quantity,
                unit_price=unit_price,
                discount=discount,
            )
        )

    db.commit()

    return OrderOut(
        order_id=order_header.id,
        customer_code=customer.code,
        order_date=str(order_header.order_date),
        delivery_date=str(order_header.delivery_date),
        status=order_header.status,
        lines=lines_out,
    )


@app.get("/orders/{order_id}", response_model=OrderOut)
def get_order(order_id: int, db: Session = Depends(get_db)):
    """
    Dettaglio di un singolo ordine.

    Mappato da:
      Service name="get_order" method="GET" path="/orders/{order_id}"
    """
    order = db.query(OrderHeader).filter(OrderHeader.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Ordine non trovato")

    customer = db.query(Customer).filter(Customer.id == order.customer_id).first()
    if not customer:
        raise HTTPException(status_code=500, detail="Cliente legato all'ordine non trovato")

    lines_out: List[OrderLineOut] = []
    for line in order.lines:
        art = db.query(Article).filter(Article.id == line.article_id).first()
        article_code = art.code if art else "???"
        lines_out.append(
            OrderLineOut(
                line_no=line.line_no,
                article_code=article_code,
                quantity=line.quantity,
                unit_price=line.unit_price,
                discount=line.discount,
            )
        )

    return OrderOut(
        order_id=order.id,
        customer_code=customer.code,
        order_date=str(order.order_date),
        delivery_date=str(order.delivery_date),
        status=order.status,
        lines=lines_out,
    )


@app.get("/orders", response_model=List[OrderOut])
def get_orders(
    customer_code: Optional[str] = Query(None),
    status: Optional[str] = Query(None, description="Filtra per stato ordine"),
    from_date: Optional[str] = Query(None, description="Data ordine minima (YYYY-MM-DD)"),
    to_date: Optional[str] = Query(None, description="Data ordine massima (YYYY-MM-DD)"),
    limit: Optional[int] = Query(10),
    db: Session = Depends(get_db),
):
    """
    Lista ordini, con filtri opzionali:

    - customer_code: per cliente
    - status: stato ordine
    - from_date / to_date: intervallo sulla data ordine
    - limit: max ordini restituiti (default 10)
    """
    q = db.query(OrderHeader)

    if customer_code:
        cust = db.query(Customer).filter(Customer.code == customer_code).first()
        if not cust:
            raise HTTPException(status_code=400, detail=f"Cliente {customer_code} non trovato")
        q = q.filter(OrderHeader.customer_id == cust.id)

    if status:
        q = q.filter(OrderHeader.status == status)

    if from_date:
        try:
            d_from = date.fromisoformat(from_date)
            q = q.filter(OrderHeader.order_date >= d_from)
        except ValueError:
            raise HTTPException(status_code=400, detail="Formato from_date non valido, usare YYYY-MM-DD")

    if to_date:
        try:
            d_to = date.fromisoformat(to_date)
            q = q.filter(OrderHeader.order_date <= d_to)
        except ValueError:
            raise HTTPException(status_code=400, detail="Formato to_date non valido, usare YYYY-MM-DD")

    q = q.order_by(OrderHeader.id.desc())
    if limit:
        q = q.limit(limit)

    orders = q.all()

    out_list: List[OrderOut] = []
    for order in orders:
        customer = db.query(Customer).filter(Customer.id == order.customer_id).first()
        if not customer:
            continue

        lines_out: List[OrderLineOut] = []
        for line in order.lines:
            art = db.query(Article).filter(Article.id == line.article_id).first()
            article_code = art.code if art else "???"
            lines_out.append(
                OrderLineOut(
                    line_no=line.line_no,
                    article_code=article_code,
                    quantity=line.quantity,
                    unit_price=line.unit_price,
                    discount=line.discount,
                )
            )

        out_list.append(
            OrderOut(
                order_id=order.id,
                customer_code=customer.code,
                order_date=str(order.order_date),
                delivery_date=str(order.delivery_date),
                status=order.status,
                lines=lines_out,
            )
        )

    return out_list


@app.delete("/orders/{order_id}", status_code=204)
def delete_order(order_id: int, db: Session = Depends(get_db)):
    """
    Cancellazione ordine (testata + righe, grazie al cascade ORM).
    """
    order = db.query(OrderHeader).filter(OrderHeader.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Ordine non trovato")

    db.delete(order)
    db.commit()
    return Response(status_code=204)
