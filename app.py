import os
from datetime import date, datetime
from decimal import Decimal
from functools import wraps
from io import BytesIO

from flask import (
    Flask,
    render_template,
    redirect,
    url_for,
    request,
    flash,
    send_file,
)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager,
    login_user,
    logout_user,
    login_required,
    current_user,
    UserMixin,
)
from passlib.hash import bcrypt
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

# -------------------------------------------------------------------
# CONFIGURACIÓN BÁSICA
# -------------------------------------------------------------------

app = Flask(__name__)

# Clave secreta (puedes cambiarla o ponerla como variable de entorno)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "super-secret-dev-key")

# IMPORTANTÍSIMO: Usar SIEMPRE SQLite para evitar psycopg2 / Postgres
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
db_path = os.path.join(BASE_DIR, "creditos.db")
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"


# -------------------------------------------------------------------
# MODELOS
# -------------------------------------------------------------------

class User(db.Model, UserMixin):
    __tablename__ = "user"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    full_name = db.Column(db.String(100), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="cobrador")  # admin / cobrador
    active = db.Column(db.Boolean, default=True)

    clientes = db.relationship("Cliente", back_populates="cobrador", lazy="dynamic")

    def set_password(self, password: str) -> None:
        self.password_hash = bcrypt.hash(password)

    def check_password(self, password: str) -> bool:
        return bcrypt.verify(password, self.password_hash)


class Cliente(db.Model):
    __tablename__ = "cliente"

    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(120), nullable=False)
    identificacion = db.Column(db.String(50))
    telefono = db.Column(db.String(50))
    activo = db.Column(db.Boolean, default=True)

    cobrador_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    cobrador = db.relationship("User", back_populates="clientes")

    movimientos = db.relationship(
        "Movimiento",
        back_populates="cliente",
        lazy="dynamic",
        cascade="all, delete-orphan",
    )

    @property
    def total_creditos(self) -> Decimal:
        total = (
            self.movimientos.filter_by(tipo="COMPRA")
            .with_entities(db.func.coalesce(db.func.sum(Movimiento.monto), 0))
            .scalar()
        )
        return Decimal(str(total or 0))

    @property
    def total_abonos(self) -> Decimal:
        total = (
            self.movimientos.filter_by(tipo="ABONO")
            .with_entities(db.func.coalesce(db.func.sum(Movimiento.monto), 0))
            .scalar()
        )
        return Decimal(str(total or 0))

    @property
    def saldo(self) -> Decimal:
        return self.total_creditos - self.total_abonos


class Movimiento(db.Model):
    __tablename__ = "movimiento"

    id = db.Column(db.Integer, primary_key=True)
    cliente_id = db.Column(db.Integer, db.ForeignKey("cliente.id"), nullable=False)
    fecha = db.Column(db.Date, nullable=False, default=date.today)
    tipo = db.Column(db.String(10), nullable=False)  # COMPRA / ABONO
    monto = db.Column(db.Numeric(10, 2), nullable=False)
    descripcion = db.Column(db.String(255))

    usuario_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    usuario = db.relationship("User")

    cliente = db.relationship("Cliente", back_populates="movimientos")


# -------------------------------------------------------------------
# LOGIN MANAGER
# -------------------------------------------------------------------

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# -------------------------------------------------------------------
# DECORADORES DE ROL
# -------------------------------------------------------------------

def admin_required(f):
    @wraps(f)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != "admin":
            flash("Solo el administrador puede acceder a esta sección.", "danger")
            return redirect(url_for("index"))
        return f(*args, **kwargs)

    return wrapper


# -------------------------------------------------------------------
# INICIALIZACIÓN DE DB Y USUARIO ADMIN
# -------------------------------------------------------------------

def init_db():
    """Crea tablas y usuario admin si no existen."""
    with app.app_context():
        db.create_all()
        admin = User.query.filter_by(username="admin").first()
        if not admin:
            admin = User(
                username="admin",
                full_name="Administrador",
                role="admin",
                active=True,
            )
            admin.set_password("admin123")
            db.session.add(admin)
            db.session.commit()
            print("✅ Usuario admin creado (admin / admin123)")
        else:
            print("✅ Usuario admin ya existe")


init_db()


# -------------------------------------------------------------------
# RUTAS DE AUTENTICACIÓN
# -------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        user = User.query.filter_by(username=username).first()
        if not user:
            flash("Usuario o contraseña incorrectos.", "danger")
            return render_template("login.html")

        if not user.active:
            flash("Este usuario está inactivo. Contacte al administrador.", "danger")
            return render_template("login.html")

        if not user.check_password(password):
            flash("Usuario o contraseña incorrectos.", "danger")
            return render_template("login.html")

        login_user(user)
        flash(f"Bienvenido, {user.full_name}", "success")
        return redirect(url_for("index"))

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Sesión cerrada correctamente.", "info")
    return redirect(url_for("login"))


# -------------------------------------------------------------------
# DASHBOARD PRINCIPAL
# -------------------------------------------------------------------

@app.route("/")
@login_required
def index():
    clientes = Cliente.query.filter_by(activo=True).all()
    total_deuda = sum((c.saldo for c in clientes), Decimal("0"))

    # Top 10 deudores (solo con saldo > 0)
    clientes_con_saldo = [c for c in clientes if c.saldo > 0]
    top10 = sorted(clientes_con_saldo, key=lambda c: c.saldo, reverse=True)[:10]

    return render_template(
        "index.html",
        total_deuda=total_deuda,
        clientes=clientes,
        top10=top10,
    )


# -------------------------------------------------------------------
# CLIENTES / COMPRADORES
# -------------------------------------------------------------------

@app.route("/clientes", methods=["GET", "POST"])
@login_required
def clientes():
    if request.method == "POST":
        nombre = request.form.get("nombre", "").strip()
        identificacion = request.form.get("identificacion", "").strip()
        telefono = request.form.get("telefono", "").strip()

        if not nombre:
            flash("El nombre del cliente es obligatorio.", "danger")
            return redirect(url_for("clientes"))

        cliente = Cliente(
            nombre=nombre,
            identificacion=identificacion or None,
            telefono=telefono or None,
            cobrador=current_user if current_user.role == "cobrador" else None,
            activo=True,
        )
        db.session.add(cliente)
        db.session.commit()
        flash("Cliente creado correctamente.", "success")
        return redirect(url_for("clientes"))

    clientes_lista = Cliente.query.order_by(Cliente.nombre).all()
    return render_template("clientes.html", clientes=clientes_lista)


@app.route("/clientes/<int:cliente_id>", methods=["GET", "POST"])
@login_required
def cliente_detalle(cliente_id):
    cliente = Cliente.query.get_or_404(cliente_id)

    if request.method == "POST":
        tipo = request.form.get("tipo")
        monto_raw = request.form.get("monto", "").replace(",", ".")
        descripcion = request.form.get("descripcion", "").strip()
        fecha_str = request.form.get("fecha", "")

        if not tipo or tipo not in ("COMPRA", "ABONO"):
            flash("Tipo de movimiento inválido.", "danger")
            return redirect(url_for("cliente_detalle", cliente_id=cliente.id))

        try:
            monto = Decimal(monto_raw)
        except Exception:
            flash("Monto inválido.", "danger")
            return redirect(url_for("cliente_detalle", cliente_id=cliente.id))

        if monto <= 0:
            flash("El monto debe ser mayor a 0.", "danger")
            return redirect(url_for("cliente_detalle", cliente_id=cliente.id))

        if fecha_str:
            try:
                fecha = datetime.strptime(fecha_str, "%Y-%m-%d").date()
            except Exception:
                fecha = date.today()
        else:
            fecha = date.today()

        mov = Movimiento(
            cliente=cliente,
            fecha=fecha,
            tipo=tipo,
            monto=monto,
            descripcion=descripcion or None,
            usuario=current_user,
        )
        db.session.add(mov)
        db.session.commit()
        flash("Movimiento registrado correctamente.", "success")
        return redirect(url_for("cliente_detalle", cliente_id=cliente.id))

    movimientos = (
        cliente.movimientos.order_by(Movimiento.fecha.desc(), Movimiento.id.desc()).all()
    )
    return render_template(
        "cliente_detalle.html",
        cliente=cliente,
        movimientos=movimientos,
    )


# Alias para compatibilidad con tu navbar anterior
@app.route("/movimientos")
@login_required
def registro_movimientos():
    return redirect(url_for("clientes"))


# -------------------------------------------------------------------
# GESTIÓN DE USUARIOS (ADMIN)
# -------------------------------------------------------------------

@app.route("/usuarios", methods=["GET", "POST"])
@admin_required
def usuarios():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        full_name = request.form.get("full_name", "").strip()
        password = request.form.get("password", "").strip()
        role = request.form.get("role", "cobrador")

        if not username or not full_name or not password:
            flash("Todos los campos son obligatorios.", "danger")
            return redirect(url_for("usuarios"))

        if role not in ("admin", "cobrador"):
            role = "cobrador"

        existing = User.query.filter_by(username=username).first()
        if existing:
            flash("Ya existe un usuario con ese nombre.", "danger")
            return redirect(url_for("usuarios"))

        user = User(
            username=username,
            full_name=full_name,
            role=role,
            active=True,
        )
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        flash("Usuario creado correctamente.", "success")
        return redirect(url_for("usuarios"))

    usuarios_lista = User.query.order_by(User.role.desc(), User.username).all()
    return render_template("usuarios_list.html", usuarios=usuarios_lista)


@app.route("/usuarios/<int:user_id>/toggle", methods=["POST"])
@admin_required
def usuario_toggle(user_id):
    user = User.query.get_or_404(user_id)
    if user.username == "admin":
        flash("No puedes desactivar al usuario admin.", "danger")
        return redirect(url_for("usuarios"))

    user.active = not user.active
    db.session.commit()
    flash("Estado del usuario actualizado.", "success")
    return redirect(url_for("usuarios"))


# -------------------------------------------------------------------
# REPORTES (SOLO ADMIN)
# -------------------------------------------------------------------

def _generar_pdf_cliente(cliente: Cliente) -> BytesIO:
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter

    # Logo (si existe static/logo.png)
    logo_path = os.path.join(app.root_path, "static", "logo.png")
    y = height - 60
    if os.path.exists(logo_path):
        try:
            c.drawImage(logo_path, 40, y - 40, width=120, preserveAspectRatio=True, mask="auto")
        except Exception:
            pass

    c.setFont("Helvetica-Bold", 14)
    c.drawString(200, y, "Estado de Cuenta - Créditos")

    y -= 60
    c.setFont("Helvetica", 11)
    c.drawString(40, y, f"Cliente: {cliente.nombre}")
    y -= 15
    if cliente.identificacion:
        c.drawString(40, y, f"Identificación: {cliente.identificacion}")
        y -= 15
    if cliente.telefono:
        c.drawString(40, y, f"Teléfono: {cliente.telefono}")
        y -= 15
    if cliente.cobrador:
        c.drawString(40, y, f"Cobrador: {cliente.cobrador.full_name}")
        y -= 15

    y -= 10
    c.setFont("Helvetica-Bold", 11)
    c.drawString(40, y, "Fecha")
    c.drawString(120, y, "Tipo")
    c.drawString(200, y, "Monto")
    c.drawString(280, y, "Descripción")
    y -= 10
    c.line(40, y, width - 40, y)
    y -= 10

    c.setFont("Helvetica", 10)
    saldo = Decimal("0")

    movimientos = (
        cliente.movimientos.order_by(Movimiento.fecha.asc(), Movimiento.id.asc()).all()
    )
    for mov in movimientos:
        if y < 80:
            c.showPage()
            y = height - 80
            c.setFont("Helvetica", 10)

        fecha_txt = mov.fecha.strftime("%d/%m/%Y")
        c.drawString(40, y, fecha_txt)
        c.drawString(120, y, "COMPRA" if mov.tipo == "COMPRA" else "ABONO")
        c.drawRightString(260, y, f"{mov.monto:,.2f}")
        if mov.descripcion:
            c.drawString(280, y, mov.descripcion[:50])

        if mov.tipo == "COMPRA":
            saldo += Decimal(str(mov.monto))
        else:
            saldo -= Decimal(str(mov.monto))

        y -= 15

    y -= 10
    c.setFont("Helvetica-Bold", 11)
    c.drawRightString(260, y, "Saldo final:")
    c.drawRightString(360, y, f"{cliente.saldo:,.2f}")

    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer


@app.route("/clientes/<int:cliente_id>/pdf")
@admin_required
def cliente_pdf(cliente_id):
    cliente = Cliente.query.get_or_404(cliente_id)
    pdf_buffer = _generar_pdf_cliente(cliente)
    filename = f"estado_{cliente.nombre.replace(' ', '_')}.pdf"
    return send_file(
        pdf_buffer,
        as_attachment=True,
        download_name=filename,
        mimetype="application/pdf",
    )


def _generar_pdf_global(clientes) -> BytesIO:
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter

    logo_path = os.path.join(app.root_path, "static", "logo.png")
    y = height - 60
    if os.path.exists(logo_path):
        try:
            c.drawImage(logo_path, 40, y - 40, width=120, preserveAspectRatio=True, mask="auto")
        except Exception:
            pass

    c.setFont("Helvetica-Bold", 14)
    c.drawString(200, y, "Reporte Global de Deudas")
    y -= 40

    total_deuda = sum((ccli.saldo for ccli in clientes), Decimal("0"))
    c.setFont("Helvetica", 11)
    c.drawString(40, y, f"Total adeudado entre todos los clientes: {total_deuda:,.2f}")
    y -= 25

    c.setFont("Helvetica-Bold", 11)
    c.drawString(40, y, "Cliente")
    c.drawRightString(260, y, "Saldo")
    y -= 10
    c.line(40, y, width - 40, y)
    y -= 10
    c.setFont("Helvetica", 10)

    for cliente in clientes:
        if y < 80:
            c.showPage()
            y = height - 80
            c.setFont("Helvetica", 10)
        c.drawString(40, y, cliente.nombre[:40])
        c.drawRightString(260, y, f"{cliente.saldo:,.2f}")
        y -= 15

    # Top 10
    clientes_con_saldo = [ccli for ccli in clientes if ccli.saldo > 0]
    top10 = sorted(clientes_con_saldo, key=lambda ccli: ccli.saldo, reverse=True)[:10]

    y -= 10
    if y < 120:
        c.showPage()
        y = height - 80

    c.setFont("Helvetica-Bold", 12)
    c.drawString(40, y, "TOP 10 CLIENTES QUE MÁS DEBEN")
    y -= 20

    c.setFont("Helvetica-Bold", 11)
    c.drawString(40, y, "Posición")
    c.drawString(120, y, "Cliente")
    c.drawRightString(260, y, "Saldo")
    y -= 10
    c.line(40, y, width - 40, y)
    y -= 10
    c.setFont("Helvetica", 10)

    for idx, cliente in enumerate(top10, start=1):
        if y < 80:
            c.showPage()
            y = height - 80
            c.setFont("Helvetica", 10)

        c.drawString(40, y, str(idx))
        c.drawString(120, y, cliente.nombre[:40])
        c.drawRightString(260, y, f"{cliente.saldo:,.2f}")
        y -= 15

    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer


@app.route("/reportes")
@admin_required
def reportes():
    clientes = Cliente.query.filter_by(activo=True).all()
    total_deuda = sum((c.saldo for c in clientes), Decimal("0"))
    clientes_con_saldo = [c for c in clientes if c.saldo > 0]
    top10 = sorted(clientes_con_saldo, key=lambda c: c.saldo, reverse=True)[:10]

    return render_template(
        "reportes.html",
        total_deuda=total_deuda,
        clientes=clientes,
        top10=top10,
    )


@app.route("/reportes/global_pdf")
@admin_required
def reporte_global_pdf():
    clientes = Cliente.query.filter_by(activo=True).all()
    pdf_buffer = _generar_pdf_global(clientes)
    return send_file(
        pdf_buffer,
        as_attachment=True,
        download_name="reporte_global.pdf",
        mimetype="application/pdf",
    )


@app.route("/reportes/global_excel")
@admin_required
def reporte_global_excel():
    from openpyxl import Workbook

    clientes = Cliente.query.filter_by(activo=True).all()

    wb = Workbook()
    ws = wb.active
    ws.title = "Deudas"

    ws.append(["Cliente", "Identificación", "Teléfono", "Saldo"])

    for cliente in clientes:
        ws.append(
            [
                cliente.nombre,
                cliente.identificacion or "",
                cliente.telefono or "",
                float(cliente.saldo),
            ]
        )

    # Hoja de Top 10
    clientes_con_saldo = [c for c in clientes if c.saldo > 0]
    top10 = sorted(clientes_con_saldo, key=lambda c: c.saldo, reverse=True)[:10]

    ws2 = wb.create_sheet(title="Top 10 Deudores")
    ws2.append(["Posición", "Cliente", "Saldo"])

    for idx, cliente in enumerate(top10, start=1):
        ws2.append([idx, cliente.nombre, float(cliente.saldo)])

    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=True,
        download_name="reporte_global.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# -------------------------------------------------------------------
# MAIN
# -------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True)

