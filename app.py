import os
from datetime import datetime
from io import BytesIO

from flask import (
    Flask, render_template, redirect, url_for,
    request, flash, send_file
)
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import (
    LoginManager, UserMixin, login_user,
    login_required, logout_user, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from dotenv import load_dotenv

# =========================
# Configuración básica
# =========================

load_dotenv()

app = Flask(__name__)

app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-key")

database_url = os.environ.get("DATABASE_URL")
if database_url:
    # Arreglo para URLs postgres antiguas
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
else:
    # Fallback local
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///creditos.db"

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
migrate = Migrate(app, db)

login_manager = LoginManager(app)
login_manager.login_view = "login"

# =========================
# Modelos
# =========================

class Usuario(db.Model, UserMixin):
    __tablename__ = "usuarios"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    es_admin = db.Column(db.Boolean, default=False)

    def set_password(self, password: str) -> None:
        """Genera el hash de la contraseña con Werkzeug."""
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        """Verifica contraseña con Werkzeug."""
        try:
            if not self.password_hash:
                print(f"[Usuario.check_password] password_hash vacío para user={self.username}")
                return False
            valido = check_password_hash(self.password_hash, password)
            print(f"[Usuario.check_password] user={self.username} valido={valido}")
            return valido
        except Exception as e:
            print(f"[Usuario.check_password] Error al verificar contraseña para user={self.username}: {e}")
            return False


class Movimiento(db.Model):
    __tablename__ = "movimientos"

    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey("usuarios.id"), nullable=False)
    fecha = db.Column(db.Date, nullable=False, default=datetime.utcnow)
    descripcion = db.Column(db.String(255), nullable=False)
    tipo = db.Column(db.String(20), nullable=False)  # "Compra" o "Pago"
    monto = db.Column(db.Numeric(10, 2), nullable=False)

    usuario = db.relationship("Usuario", backref=db.backref("movimientos", lazy=True))


@login_manager.user_loader
def load_user(user_id):
    return Usuario.query.get(int(user_id))


# =========================
# Funciones de ayuda
# =========================

def calcular_saldo(usuario: Usuario) -> float:
    saldo = 0.0
    for mov in usuario.movimientos:
        if mov.tipo == "Compra":
            saldo += float(mov.monto)
        else:  # Pago u otro
            saldo -= float(mov.monto)
    return saldo


def generar_pdf_usuario(usuario: Usuario):
    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter

    y = height - 50
    p.setFont("Helvetica-Bold", 14)
    p.drawString(50, y, f"Reporte de movimientos - Usuario: {usuario.username}")
    y -= 30

    p.setFont("Helvetica", 10)
    for mov in usuario.movimientos:
        linea = f"{mov.fecha} - {mov.descripcion} - {mov.tipo} - {mov.monto}"
        p.drawString(50, y, linea)
        y -= 15
        if y < 50:
            p.showPage()
            y = height - 50
            p.setFont("Helvetica", 10)

    y -= 20
    saldo = calcular_saldo(usuario)
    p.setFont("Helvetica-Bold", 12)
    p.drawString(50, y, f"Saldo actual: {saldo:.2f}")

    p.showPage()
    p.save()
    buffer.seek(0)
    return buffer


# =========================
# Rutas
# =========================

@app.route("/")
@login_required
def index():
    # Redirige a la pantalla principal de movimientos
    return redirect(url_for("registro_movimientos"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    error = None

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = Usuario.query.filter_by(username=username).first()

        if user and user.check_password(password):
            login_user(user)
            flash("Sesión iniciada correctamente.", "success")
            next_page = request.args.get("next")
            return redirect(next_page or url_for("index"))
        else:
            error = "Usuario o contraseña incorrectos."

    return render_template("login.html", error=error)


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Sesión finalizada.", "info")
    return redirect(url_for("login"))


@app.route("/movimientos", methods=["GET", "POST"])
@login_required
def registro_movimientos():
    # POST: registrar movimiento
    if request.method == "POST":
        if current_user.es_admin:
            usuario_id = request.form.get("usuario_id")
        else:
            usuario_id = current_user.id

        descripcion = request.form.get("descripcion", "").strip()
        tipo = request.form.get("tipo", "Compra")
        monto_str = request.form.get("monto", "0").replace(",", ".")
        fecha_str = request.form.get("fecha")

        try:
            monto = float(monto_str)
        except ValueError:
            flash("Monto inválido.", "danger")
            return redirect(url_for("registro_movimientos"))

        if fecha_str:
            try:
                fecha = datetime.strptime(fecha_str, "%Y-%m-%d").date()
            except ValueError:
                fecha = datetime.utcnow().date()
        else:
            fecha = datetime.utcnow().date()

        if not descripcion:
            flash("La descripción es obligatoria.", "danger")
            return redirect(url_for("registro_movimientos"))

        mov = Movimiento(
            usuario_id=int(usuario_id),
            descripcion=descripcion,
            tipo=tipo,
            monto=monto,
            fecha=fecha,
        )
        db.session.add(mov)
        db.session.commit()
        flash("Movimiento registrado correctamente.", "success")
        return redirect(url_for("registro_movimientos"))

    # GET: mostrar movimientos
    if current_user.es_admin:
        usuario_id = request.args.get("usuario_id", type=int)
        if usuario_id:
            movimientos = (
                Movimiento.query.filter_by(usuario_id=usuario_id)
                .order_by(Movimiento.fecha.desc())
                .all()
            )
            usuario_actual = Usuario.query.get(usuario_id)
        else:
            movimientos = Movimiento.query.order_by(Movimiento.fecha.desc()).all()
            usuario_actual = None
        usuarios = Usuario.query.order_by(Usuario.username).all()
    else:
        movimientos = (
            Movimiento.query.filter_by(usuario_id=current_user.id)
            .order_by(Movimiento.fecha.desc())
            .all()
        )
        usuario_actual = current_user
        usuarios = None

    if current_user.es_admin and not usuario_actual:
        saldo = None
    else:
        saldo = calcular_saldo(usuario_actual)

    return render_template(
        "movimientos.html",
        movimientos=movimientos,
        usuarios=usuarios,
        usuario_actual=usuario_actual,
        saldo=saldo,
    )


@app.route("/usuarios")
@login_required
def usuarios_list():
    if not current_user.es_admin:
        flash("Solo el administrador puede ver la lista de usuarios.", "danger")
        return redirect(url_for("index"))

    usuarios = Usuario.query.order_by(Usuario.username).all()
    data = [
        {
            "id": u.id,
            "username": u.username,
            "saldo": calcular_saldo(u),
        }
        for u in usuarios
    ]

    return render_template("usuarios_list.html", usuarios=data)


@app.route("/usuarios/<int:usuario_id>")
@login_required
def usuario_detalle(usuario_id):
    if not current_user.es_admin and current_user.id != usuario_id:
        flash("No tienes permiso para ver este usuario.", "danger")
        return redirect(url_for("index"))

    usuario = Usuario.query.get_or_404(usuario_id)
    movimientos = (
        Movimiento.query.filter_by(usuario_id=usuario.id)
        .order_by(Movimiento.fecha.desc())
        .all()
    )
    saldo = calcular_saldo(usuario)

    return render_template(
        "usuario.html",
        usuario=usuario,
        movimientos=movimientos,
        saldo=saldo,
    )


@app.route("/usuarios/<int:usuario_id>/pdf")
@login_required
def usuario_pdf(usuario_id):
    if not current_user.es_admin and current_user.id != usuario_id:
        flash("No tienes permiso para descargar este PDF.", "danger")
        return redirect(url_for("index"))

    usuario = Usuario.query.get_or_404(usuario_id)
    buffer = generar_pdf_usuario(usuario)

    filename = f"reporte_{usuario.username}.pdf"
    return send_file(
        buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
    )


# =========================
# Inicialización BD y admin
# =========================

with app.app_context():
    db.create_all()
    print("✅ Tablas verificadas o creadas correctamente.")

    try:
        admin = Usuario.query.filter_by(username="admin").first()
        if not admin:
            admin = Usuario(username="admin", es_admin=True)
            db.session.add(admin)

        # Siempre asegura que la contraseña del admin sea admin123
        admin.set_password("admin123")

        db.session.commit()
        print("✅ Usuario admin listo (usuario: admin / contraseña: admin123).")
    except Exception as e:
        print(f"[INIT] Error al crear/actualizar admin: {e}")
        db.session.rollback()


if __name__ == "__main__":
    app.run(debug=True)



