import os
from decimal import Decimal
from datetime import date

from flask import (
    Flask,
    render_template,
    redirect,
    url_for,
    request,
    flash,
    send_file,
    abort,
)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager,
    UserMixin,
    login_user,
    logout_user,
    login_required,
    current_user,
)
from werkzeug.security import generate_password_hash, check_password_hash

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter

# -------------------------------------------------------------------
# Configuración básica
# -------------------------------------------------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "cambia-esta-clave")

# ==== BASE DE DATOS: FORZAR SIEMPRE SQLITE (SIN POSTGRES) ==========
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
db_path = os.path.join(BASE_DIR, "creditos.db")

# Usar SIEMPRE SQLite, ignorando DATABASE_URL de Render
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
# ===================================================================

db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"


# -------------------------------------------------------------------
# Modelos
# -------------------------------------------------------------------
class Usuario(UserMixin, db.Model):
    __tablename__ = "usuarios"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    es_admin = db.Column(db.Boolean, default=False)
    activo = db.Column(db.Boolean, default=True)

    def set_password(self, password: str):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class Cliente(db.Model):
    __tablename__ = "clientes"

    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(120), nullable=False)
    telefono = db.Column(db.String(50), nullable=True)
    saldo = db.Column(db.Numeric(12, 2), default=0)

    movimientos = db.relationship(
        "Movimiento", backref="cliente", lazy=True, cascade="all, delete-orphan"
    )


class Movimiento(db.Model):
    __tablename__ = "movimientos"

    id = db.Column(db.Integer, primary_key=True)
    cliente_id = db.Column(db.Integer, db.ForeignKey("clientes.id"), nullable=False)
    tipo = db.Column(db.String(10), nullable=False)  # COMPRA o ABONO
    monto = db.Column(db.Numeric(12, 2), nullable=False)
    fecha = db.Column(db.Date, nullable=False, default=date.today)
    descripcion = db.Column(db.String(255), nullable=True)


# -------------------------------------------------------------------
# Login manager
# -------------------------------------------------------------------
@login_manager.user_loader
def load_user(user_id):
    return Usuario.query.get(int(user_id))


# -------------------------------------------------------------------
# Decorador simple para exigir usuario activo
# -------------------------------------------------------------------
def login_required_activo(f):
    from functools import wraps

    @wraps(f)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.activo:
            logout_user()
            flash("Tu usuario está inactivo. Contacta al administrador.", "danger")
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return wrapper


# -------------------------------------------------------------------
# Rutas
# -------------------------------------------------------------------
@app.route("/")
@login_required_activo
def index():
    total_clientes = Cliente.query.count()
    total_usuarios = Usuario.query.count()
    total_saldo = db.session.query(db.func.coalesce(db.func.sum(Cliente.saldo), 0)).scalar()

    return render_template(
        "index.html",
        total_clientes=total_clientes,
        total_usuarios=total_usuarios,
        total_saldo=total_saldo,
    )


# ---------------------- LOGIN / LOGOUT ------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"].strip()

        usuario = Usuario.query.filter_by(username=username).first()
        if usuario and usuario.check_password(password) and usuario.activo:
            login_user(usuario)
            flash("Bienvenido.", "success")
            next_page = request.args.get("next")
            return redirect(next_page or url_for("index"))
        else:
            flash("Usuario o contraseña incorrectos, o usuario inactivo.", "danger")

    return render_template("login.html")


@app.route("/logout")
@login_required_activo
def logout():
    logout_user()
    return redirect(url_for("login"))


# -------------------------- CLIENTES -------------------------------
@app.route("/clientes", methods=["GET", "POST"])
@login_required_activo
def clientes():
    if request.method == "POST":
        nombre = request.form["nombre"].strip()
        telefono = request.form.get("telefono", "").strip()

        if not nombre:
            flash("El nombre del cliente es obligatorio.", "danger")
        else:
            cliente = Cliente(nombre=nombre, telefono=telefono)
            db.session.add(cliente)
            db.session.commit()
            flash("Cliente creado correctamente.", "success")
            return redirect(url_for("clientes"))

    clientes_list = Cliente.query.order_by(Cliente.nombre).all()
    return render_template("clientes.html", clientes=clientes_list)


@app.route("/clientes/<int:cliente_id>", methods=["GET", "POST"])
@login_required_activo
def cliente_detalle(cliente_id):
    cliente = Cliente.query.get_or_404(cliente_id)

    if request.method == "POST":
        tipo = request.form["tipo"]
        monto_str = request.form["monto"]
        fecha_str = request.form.get("fecha") or date.today().isoformat()
        descripcion = request.form.get("descripcion", "").strip()

        try:
            monto = Decimal(monto_str)
        except Exception:
            flash("Monto inválido.", "danger")
            return redirect(url_for("cliente_detalle", cliente_id=cliente.id))

        fecha_mov = date.fromisoformat(fecha_str)

        mov = Movimiento(
            cliente_id=cliente.id,
            tipo=tipo,
            monto=monto,
            fecha=fecha_mov,
            descripcion=descripcion or None,
        )
        db.session.add(mov)

        # Actualizar saldo del cliente
        if tipo == "COMPRA":
            cliente.saldo = (cliente.saldo or 0) + monto
        else:  # ABONO
            cliente.saldo = (cliente.saldo or 0) - monto

        db.session.commit()
        flash("Movimiento registrado correctamente.", "success")
        return redirect(url_for("cliente_detalle", cliente_id=cliente.id))

    movimientos = (
        Movimiento.query.filter_by(cliente_id=cliente.id)
        .order_by(Movimiento.fecha.desc(), Movimiento.id.desc())
        .all()
    )

    return render_template(
        "cliente_detalle.html",
        cliente=cliente,
        movimientos=movimientos,
        date=date,  # para usar date.today en el template
    )


@app.route("/clientes/<int:cliente_id>/pdf")
@login_required_activo
def cliente_pdf(cliente_id):
    cliente = Cliente.query.get_or_404(cliente_id)
    movimientos = (
        Movimiento.query.filter_by(cliente_id=cliente.id)
        .order_by(Movimiento.fecha)
        .all()
    )

    pdf_path = f"/tmp/cliente_{cliente.id}.pdf"
    c = canvas.Canvas(pdf_path, pagesize=letter)
    width, height = letter

    # Logo
    logo_path = os.path.join(app.root_path, "static", "ibafuco_logo.jpg")
    if os.path.exists(logo_path):
        c.drawImage(
            logo_path,
            40,
            height - 120,
            width=120,
            height=80,
            preserveAspectRatio=True,
            mask="auto",
        )

    y = height - 140
    c.setFont("Helvetica-Bold", 14)
    c.drawString(40, y, f"Estado de cuenta - {cliente.nombre}")
    y -= 20

    c.setFont("Helvetica", 11)
    c.drawString(40, y, f"Teléfono: {cliente.telefono or 'No registrado'}")
    y -= 20
    c.drawString(40, y, f"Saldo actual: ₡{cliente.saldo:.2f}")
    y -= 30

    c.setFont("Helvetica-Bold", 12)
    c.drawString(40, y, "Movimientos:")
    y -= 20

    c.setFont("Helvetica", 10)
    for mov in movimientos:
        if y < 50:
            c.showPage()
            y = height - 50
            c.setFont("Helvetica", 10)

        tipo = "COMPRA" if mov.tipo == "COMPRA" else "ABONO"
        linea = (
            f"{mov.fecha.strftime('%d/%m/%Y')} - {tipo} - "
            f"₡{mov.monto:.2f} - {mov.descripcion or ''}"
        )
        c.drawString(40, y, linea)
        y -= 15

    c.showPage()
    c.save()

    return send_file(
        pdf_path,
        as_attachment=True,
        download_name=f"cliente_{cliente.nombre}.pdf",
    )


# -------------------------- USUARIOS -------------------------------
@app.route("/usuarios", methods=["GET", "POST"])
@login_required_activo
def usuarios():
    if not current_user.es_admin:
        abort(403)

    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"].strip()
        es_admin = bool(request.form.get("es_admin"))

        if not username or not password:
            flash("Usuario y contraseña son obligatorios.", "danger")
        elif Usuario.query.filter_by(username=username).first():
            flash("Ya existe un usuario con ese nombre.", "danger")
        else:
            u = Usuario(username=username, es_admin=es_admin, activo=True)
            u.set_password(password)
            db.session.add(u)
            db.session.commit()
            flash("Usuario creado correctamente.", "success")
            return redirect(url_for("usuarios"))

    usuarios_list = Usuario.query.order_by(Usuario.username).all()
    return render_template("usuarios_list.html", usuarios=usuarios_list)


@app.route("/usuarios/<int:usuario_id>/toggle", methods=["POST"])
@login_required_activo
def toggle_usuario(usuario_id):
    if not current_user.es_admin:
        abort(403)

    u = Usuario.query.get_or_404(usuario_id)
    if u.id == current_user.id and u.es_admin:
        flash("No puedes desactivar tu propio usuario admin.", "danger")
    else:
        u.activo = not u.activo
        db.session.commit()
        flash("Estado de usuario actualizado.", "success")

    return redirect(url_for("usuarios"))


@app.route("/usuarios/<int:usuario_id>/reset_password", methods=["POST"])
@login_required_activo
def reset_password(usuario_id):
    if not current_user.es_admin:
        abort(403)

    u = Usuario.query.get_or_404(usuario_id)
    nueva = "123456"
    u.set_password(nueva)
    db.session.commit()
    flash(
        f"Contraseña de {u.username} restablecida a: {nueva}. "
        "Pídele que la cambie al ingresar.",
        "warning",
    )
    return redirect(url_for("usuarios"))


# ------------------ CAMBIAR PASSWORD (usuario actual) --------------
@app.route("/cambiar_password", methods=["GET", "POST"])
@login_required_activo
def cambiar_password():
    if request.method == "POST":
        actual = request.form["actual"]
        nueva = request.form["nueva"]
        confirmar = request.form["confirmar"]

        if not current_user.check_password(actual):
            flash("La contraseña actual no es correcta.", "danger")
        elif nueva != confirmar:
            flash("La nueva contraseña y la confirmación no coinciden.", "danger")
        elif len(nueva) < 6:
            flash("La nueva contraseña debe tener al menos 6 caracteres.", "danger")
        else:
            current_user.set_password(nueva)
            db.session.commit()
            flash("Contraseña actualizada correctamente.", "success")
            return redirect(url_for("index"))

    return render_template("cambiar_password.html")


# -------------------------------------------------------------------
# Crear admin al inicio si no existe
# -------------------------------------------------------------------
def crear_admin_si_no_existe():
    with app.app_context():
        db.create_all()
        admin = Usuario.query.filter_by(username="admin").first()
        if not admin:
            admin = Usuario(username="admin", es_admin=True, activo=True)
            admin.set_password("admin123")
            db.session.add(admin)
            db.session.commit()
            print("✅ Usuario admin creado (admin / admin123)")
        else:
            print("✅ Usuario admin ya existe")


crear_admin_si_no_existe()


# -------------------------------------------------------------------
# Para desarrollo local
# -------------------------------------------------------------------
if __name__ == "__main__":
    app.run(debug=True)


