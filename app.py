import os
from datetime import datetime

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
    UserMixin,
    login_user,
    logout_user,
    login_required,
    current_user,
)
from passlib.hash import bcrypt
from fpdf import FPDF

# -------------------------------------------------
# Configuración básica de Flask y base de datos
# -------------------------------------------------
app = Flask(__name__)

# Clave secreta (puedes usar la de tu variable de entorno en Render)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-key")

# URL de la base de datos: usa DATABASE_URL si existe (Render/Neon),
# si no, usa un SQLite local.
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL", "sqlite:///creditos.db"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"


# -------------------------------------------------
# Modelos
# -------------------------------------------------
class Usuario(db.Model, UserMixin):
    __tablename__ = "usuarios"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    full_name = db.Column(db.String(150))
    password_hash = db.Column(db.String(255), nullable=False)
    # admin, cobrador, cliente
    role = db.Column(db.String(20), default="cliente")

    movimientos = db.relationship("Movimiento", backref="usuario", lazy=True)

    # --------- Métodos de utilidad ---------
    def set_password(self, password: str) -> None:
        self.password_hash = bcrypt.hash(password)

    def check_password(self, password: str) -> bool:
        try:
            valido = bcrypt.verify(password, self.password_hash)
            print(f"[Usuario.check_password] user={self.username} valido={valido}")
            return valido
        except Exception as e:
            print(f"[Usuario.check_password] Error: {e}")
            return False

    def is_admin(self) -> bool:
        return self.role == "admin"

    def is_cobrador(self) -> bool:
        return self.role == "cobrador"

    def saldo(self) -> float:
        """Calcula el saldo del usuario (compras suman, abonos restan)."""
        total = 0.0
        for mov in self.movimientos:
            if mov.tipo == "Compra":
                total += float(mov.monto)
            else:
                total -= float(mov.monto)
        return total


class Movimiento(db.Model):
    __tablename__ = "movimientos"

    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(
        db.Integer, db.ForeignKey("usuarios.id"), nullable=False
    )
    fecha = db.Column(db.Date, nullable=False)
    descripcion = db.Column(db.String(255))
    # "Compra" o "Abono"
    tipo = db.Column(db.String(20), nullable=False)
    monto = db.Column(db.Float, nullable=False)


# -------------------------------------------------
# Login manager
# -------------------------------------------------
@login_manager.user_loader
def load_user(user_id):
    return Usuario.query.get(int(user_id))


# -------------------------------------------------
# Inicialización de la base de datos
#   - Crea tablas
#   - Crea admin
#   - Crea cobradores Felipe y Jorge
# -------------------------------------------------
def inicializar_datos():
    db.create_all()

    # Admin
    admin = Usuario.query.filter_by(username="admin").first()
    if not admin:
        admin = Usuario(
            username="admin",
            full_name="Administrador",
            role="admin",
        )
        admin.set_password("admin123")
        db.session.add(admin)

    # Cobrador Felipe
    felipe = Usuario.query.filter_by(username="felipe").first()
    if not felipe:
        felipe = Usuario(
            username="felipe",
            full_name="Felipe Cobrador",
            role="cobrador",
        )
        felipe.set_password("felipe123")
        db.session.add(felipe)

    # Cobrador Jorge
    jorge = Usuario.query.filter_by(username="jorge").first()
    if not jorge:
        jorge = Usuario(
            username="jorge",
            full_name="Jorge Cobrador",
            role="cobrador",
        )
        jorge.set_password("jorge123")
        db.session.add(jorge)

    db.session.commit()
    print("✅ Tablas y usuarios base listos (admin, felipe, jorge).")


with app.app_context():
    inicializar_datos()


# -------------------------------------------------
# Rutas de autenticación
# -------------------------------------------------
@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("movimientos"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        user = Usuario.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user)
            flash("Sesión iniciada correctamente.", "success")
            return redirect(url_for("movimientos"))
        else:
            flash("Usuario o contraseña incorrectos.", "danger")

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Sesión cerrada.", "info")
    return redirect(url_for("login"))


# -------------------------------------------------
# Rutas para clientes (compradores)
# -------------------------------------------------
def requiere_admin_o_cobrador():
    """Devuelve True si el usuario es admin o cobrador."""
    return current_user.is_authenticated and (
        current_user.is_admin() or current_user.is_cobrador()
    )


@app.route("/clientes")
@login_required
def clientes_list():
    if not requiere_admin_o_cobrador():
        flash("No tiene permisos para ver los clientes.", "danger")
        return redirect(url_for("movimientos"))

    clientes = (
        Usuario.query.filter(Usuario.role == "cliente")
        .order_by(Usuario.username.asc())
        .all()
    )
    return render_template("clientes_list.html", clientes=clientes)


@app.route("/clientes/nuevo", methods=["GET", "POST"])
@login_required
def cliente_nuevo():
    if not requiere_admin_o_cobrador():
        flash("No tiene permisos para crear clientes.", "danger")
        return redirect(url_for("movimientos"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        full_name = request.form.get("full_name", "").strip()

        if not username:
            flash("El usuario es obligatorio.", "danger")
            return render_template("cliente_form.html")

        existente = Usuario.query.filter_by(username=username).first()
        if existente:
            flash("Ese usuario ya existe.", "danger")
            return render_template("cliente_form.html")

        cliente = Usuario(
            username=username,
            full_name=full_name or None,
            role="cliente",
        )
        # Si quisieras que el cliente pueda iniciar sesión, aquí se le pondría una contraseña
        cliente.set_password("1234")  # contraseña genérica
        db.session.add(cliente)
        db.session.commit()

        flash("Cliente creado correctamente.", "success")
        return redirect(url_for("clientes_list"))

    return render_template("cliente_form.html")


# -------------------------------------------------
# Registro de movimientos
# -------------------------------------------------
@app.route("/movimientos", methods=["GET", "POST"])
@login_required
def movimientos():
    if not requiere_admin_o_cobrador():
        flash(
            "Solo el administrador o los cobradores pueden registrar movimientos.",
            "danger",
        )
        return redirect(url_for("clientes_list"))

    # Crear movimiento
    if request.method == "POST":
        usuario_id = request.form.get("usuario_id")
        fecha_str = request.form.get("fecha")
        descripcion = request.form.get("descripcion", "").strip()
        tipo = request.form.get("tipo")
        monto_str = request.form.get("monto")

        if not usuario_id or not fecha_str or not tipo or not monto_str:
            flash("Todos los campos son obligatorios.", "danger")
        else:
            try:
                fecha = datetime.strptime(fecha_str, "%Y-%m-%d").date()
                monto = float(monto_str)
                mov = Movimiento(
                    usuario_id=int(usuario_id),
                    fecha=fecha,
                    descripcion=descripcion or "",
                    tipo=tipo,
                    monto=monto,
                )
                db.session.add(mov)
                db.session.commit()
                flash("Movimiento registrado correctamente.", "success")
            except Exception as e:
                print(f"[movimientos] Error al guardar: {e}")
                flash("Ocurrió un error al guardar el movimiento.", "danger")

    # Filtro por usuario en la tabla
    filtro_usuario = request.args.get("filtro_usuario", type=int)

    clientes = (
        Usuario.query.filter(Usuario.role == "cliente")
        .order_by(Usuario.username.asc())
        .all()
    )

    query_movs = Movimiento.query.join(Usuario).order_by(Movimiento.fecha.desc())

    if filtro_usuario:
        query_movs = query_movs.filter(Movimiento.usuario_id == filtro_usuario)

    movimientos = query_movs.all()

    return render_template(
        "movimientos.html",
        clientes=clientes,
        movimientos=movimientos,
        filtro_usuario=filtro_usuario,
    )


# -------------------------------------------------
# Detalle de usuario y PDF
# -------------------------------------------------
@app.route("/usuario/<int:usuario_id>")
@login_required
def usuario_detalle(usuario_id):
    if not requiere_admin_o_cobrador():
        flash("No tiene permisos para ver el detalle de usuarios.", "danger")
        return redirect(url_for("movimientos"))

    usuario = Usuario.query.get_or_404(usuario_id)
    movimientos = (
        Movimiento.query.filter_by(usuario_id=usuario_id)
        .order_by(Movimiento.fecha.desc())
        .all()
    )
    saldo = usuario.saldo()

    return render_template(
        "usuario.html",
        usuario=usuario,
        movimientos=movimientos,
        saldo=saldo,
    )


@app.route("/usuario/<int:usuario_id>/pdf")
@login_required
def usuario_pdf(usuario_id):
    if not requiere_admin_o_cobrador():
        flash("No tiene permisos para descargar reportes.", "danger")
        return redirect(url_for("movimientos"))

    usuario = Usuario.query.get_or_404(usuario_id)
    movimientos = (
        Movimiento.query.filter_by(usuario_id=usuario_id)
        .order_by(Movimiento.fecha.desc())
        .all()
    )

    # Crear PDF
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    pdf.set_font("Arial", "B", 16)
    pdf.cell(0, 10, "Créditos IBafuco", ln=True, align="C")
    pdf.ln(4)

    pdf.set_font("Arial", "B", 14)
    nombre_mostrar = usuario.full_name or usuario.username
    pdf.cell(0, 10, f"Reporte de movimientos - {nombre_mostrar}", ln=True, align="C")

    pdf.ln(8)
    pdf.set_font("Arial", size=11)
    pdf.cell(0, 8, f"Usuario: {usuario.username}", ln=True)
    if usuario.full_name:
        pdf.cell(0, 8, f"Nombre completo: {usuario.full_name}", ln=True)

    saldo = usuario.saldo()
    pdf.cell(0, 8, f"Saldo actual: {saldo:.2f}", ln=True)

    pdf.ln(8)
    pdf.set_font("Arial", "B", 12)
    pdf.cell(0, 8, "Movimientos:", ln=True)
    pdf.ln(4)

    # Encabezados de tabla
    pdf.set_font("Arial", "B", 11)
    pdf.cell(30, 8, "Fecha", border=1)
    pdf.cell(70, 8, "Descripción", border=1)
    pdf.cell(25, 8, "Tipo", border=1)
    pdf.cell(30, 8, "Monto", border=1, ln=True)

    # Filas
    pdf.set_font("Arial", size=10)
    if movimientos:
        for mov in movimientos:
            fecha_str = mov.fecha.strftime("%d/%m/%Y")
            desc = (mov.descripcion or "")[:35]
            pdf.cell(30, 8, fecha_str, border=1)
            pdf.cell(70, 8, desc, border=1)
            pdf.cell(25, 8, mov.tipo, border=1)
            pdf.cell(30, 8, f"{mov.monto:.2f}", border=1, ln=True)
    else:
        pdf.ln(4)
        pdf.cell(0, 8, "No hay movimientos registrados para este usuario.", ln=True)

    filename = f"reporte_{usuario.username}.pdf"
    pdf.output(filename)

    return send_file(filename, as_attachment=True)


# -------------------------------------------------
# Lista general de usuarios (solo admin)
# -------------------------------------------------
@app.route("/usuarios")
@login_required
def usuarios_list():
    if not current_user.is_admin():
        flash("Solo el administrador puede ver la lista de usuarios.", "danger")
        return redirect(url_for("movimientos"))

    usuarios = Usuario.query.order_by(Usuario.username.asc()).all()
    return render_template("usuarios_list.html", usuarios=usuarios)


# -------------------------------------------------
# Punto de entrada
# -------------------------------------------------
if __name__ == "__main__":
    app.run(debug=True)

