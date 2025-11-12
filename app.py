import os
from flask import Flask, render_template, request, redirect, url_for, flash
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from passlib.hash import bcrypt
import psycopg
from psycopg.rows import dict_row
from flask_talisman import Talisman
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_seasurf import SeaSurf
from dotenv import load_dotenv

# ----------------------------
# Configuración inicial
# ----------------------------
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "secret_key")

# Seguridad y rate limiting
Talisman(app, content_security_policy=None)
csrf = SeaSurf(app)
limiter = Limiter(get_remote_address, app=app)

# Configuración de base de datos (Neon o local)
DB_URL = os.getenv("DATABASE_URL")

# ----------------------------
# Flask-Login setup
# ----------------------------
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"


class Usuario(UserMixin):
    def __init__(self, id, username, password_hash, role):
        self.id = id
        self.username = username
        self.password_hash = password_hash
        self.role = role


@login_manager.user_loader
def load_user(user_id):
    with psycopg.connect(DB_URL, row_factory=dict_row) as conn:
        user = conn.execute(
            "SELECT * FROM usuarios WHERE id = %s", (user_id,)
        ).fetchone()
        if user:
            return Usuario(user["id"], user["username"], user["password_hash"], user["role"])
    return None


# ----------------------------
# Crear tablas si no existen
# ----------------------------
def crear_tablas():
    with psycopg.connect(DB_URL) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS usuarios (
                id SERIAL PRIMARY KEY,
                username VARCHAR(50) UNIQUE NOT NULL,
                password_hash VARCHAR(128) NOT NULL,
                role VARCHAR(20) DEFAULT 'usuario'
            );
        """)
        conn.commit()
    print("✅ Tablas verificadas o creadas correctamente.")


# ----------------------------
# Rutas
# ----------------------------
@app.route("/")
@login_required
def index():
    return render_template("form.html", usuario=current_user.username)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]

        # 🔒 Truncar a 72 bytes para evitar error bcrypt
        password = (password or "").encode("utf-8")[:72].decode("utf-8", "ignore")

        with psycopg.connect(DB_URL, row_factory=dict_row) as conn:
            user = conn.execute(
                "SELECT * FROM usuarios WHERE username = %s",
                (username,)
            ).fetchone()

        if user and bcrypt.verify(password, user["password_hash"]):
            user_obj = Usuario(user["id"], user["username"], user["password_hash"], user["role"])
            login_user(user_obj)
            flash("Inicio de sesión exitoso.", "success")
            return redirect(url_for("index"))
        else:
            flash("Usuario o contraseña incorrectos.", "error")

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Has cerrado sesión correctamente.", "info")
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]

        # 🔒 Truncar también al crear usuario
        password = (password or "").encode("utf-8")[:72].decode("utf-8", "ignore")
        hashed = bcrypt.hash(password)

        try:
            with psycopg.connect(DB_URL) as conn:
                conn.execute(
                    "INSERT INTO usuarios (username, password_hash) VALUES (%s, %s)",
                    (username, hashed),
                )
                conn.commit()
            flash("Usuario registrado con éxito. Inicia sesión.", "success")
            return redirect(url_for("login"))
        except Exception as e:
            flash(f"Error al registrar usuario: {e}", "error")

    return render_template("register.html")


# ----------------------------
# Health check (para Render)
# ----------------------------
@app.route("/health")
def health():
    return "OK", 200


# ----------------------------
# Main
# ----------------------------
if __name__ == "__main__":
    crear_tablas()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

