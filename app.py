import os, base64, firebase_admin
from datetime import datetime, timedelta, timezone
from firebase_admin import credentials, firestore
from flask import Flask, render_template, request, redirect, url_for, flash, session
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "clave-local-aulaflow")
ZONA_HORARIA_OAXACA = timezone(timedelta(hours=-6))
CODIGO_ADMIN = os.environ.get("CODIGO_ADMIN")
if CODIGO_ADMIN is None:
    if os.environ.get("RENDER"):
        CODIGO_ADMIN = ""
    else:
        CODIGO_ADMIN = "profesor123cecyte"

COLECCION_USUARIOS = "Usuarios"
COLECCION_CLASES = "Clases"
COLECCION_CODIGOS_CLASES = "CodigosClases"

if not firebase_admin._apps:
    ruta_credenciales = os.environ.get("FIREBASE_CREDENTIALS_FILE", "firebase_credentials.json")
    cred = credentials.Certificate(ruta_credenciales)
    firebase_admin.initialize_app(cred)

db = firestore.client()

# Firestore guarda fechas en UTC; aquí las pasamos a la hora local al pintarlas.
def hora_oaxaca(valor, formato="%d/%m/%Y a las %H:%M"):
    """Formatea fechas almacenadas en UTC usando la hora local de Oaxaca."""
    if not isinstance(valor, datetime):
        return ""
    # Las fechas sin zona horaria se consideran UTC porque Firestore las guarda así.
    if valor.tzinfo is None:
        valor = valor.replace(tzinfo=timezone.utc)
    return valor.astimezone(ZONA_HORARIA_OAXACA).strftime(formato)


app.jinja_env.filters["hora_oaxaca"] = hora_oaxaca

def obtener_clase(clase_id):
    snapshot = db.collection(COLECCION_CLASES).document(clase_id).get()
    if not snapshot.exists:
        return None
    datos = snapshot.to_dict() or {}
    datos["id"] = snapshot.id
    return datos


def clave_orden_clase(clase):
    nombre = clase.get("nombre", "").casefold()
    return nombre, clase["id"]


def clases_de_usuario(rol):
    if rol == "profesor":
        campo = "profesor_id"
        comparador = "=="
    else:
        campo = "alumnos_ids"
        comparador = "array_contains"

    consulta = db.collection(COLECCION_CLASES).where(campo, comparador, session["usuario_id"])
    clases = []
    for snapshot in consulta.stream():
        clase = snapshot.to_dict() or {}
        clase["id"] = snapshot.id
        fila_snapshot = obtener_fila_actual(snapshot.reference)
        if fila_snapshot:
            clase["fila"] = fila_snapshot.to_dict()
            clase["fila"]["id"] = fila_snapshot.id
        else:
            clase["fila"] = None
        if rol == "profesor":
            clase["alumnos_total"] = len(clase.get("alumnos_ids", []))
        clases.append(clase)

    clases.sort(key=clave_orden_clase)
    return clases


@app.context_processor
def clases_para_navegacion():
    """Comparte las clases del usuario con la barra lateral de las vistas autenticadas."""
    usuario_id = session.get("usuario_id")
    rol = session.get("tipo_usuario")
    clase_actual_id = (request.view_args or {}).get("clase_id")
    if not usuario_id or rol not in ("alumno", "profesor"):
        return {"clases_menu": [], "rol_menu": rol, "clase_actual_id": clase_actual_id}

    if rol == "profesor":
        campo = "profesor_id"
        comparador = "=="
    else:
        campo = "alumnos_ids"
        comparador = "array_contains"

    consulta = db.collection(COLECCION_CLASES).where(campo, comparador, usuario_id)
    clases_menu = []
    for snapshot in consulta.stream():
        datos = snapshot.to_dict() or {}
        clases_menu.append({
            "id": snapshot.id,
            "nombre": datos.get("nombre", "Clase"),
            "materia": datos.get("materia", ""),
            "foto_clase": datos.get("foto_clase", ""),
        })

    clases_menu.sort(key=clave_orden_clase)
    return {"clases_menu": clases_menu, "rol_menu": rol, "clase_actual_id": clase_actual_id}


def obtener_fila_actual(referencia_clase):
    filas = referencia_clase.collection("filas").where(
        "estado", "in", ["abierta", "pausada"]
    ).stream()
    return max(
        filas,
        key=lambda fila: (fila.to_dict() or {}).get("iniciada_en")
        or datetime.min.replace(tzinfo=timezone.utc),
        default=None,
    )


def obtener_alumnos_clase(clase):
    alumnos = []
    for alumno_id in clase.get("alumnos_ids", []):
        documento = db.collection(COLECCION_USUARIOS).document(alumno_id).get()
        if documento.exists:
            alumno = documento.to_dict() or {}
            alumno["id"] = documento.id
            alumnos.append(alumno)
    alumnos.sort(key=clave_orden_alumno)
    return alumnos


def clave_orden_alumno(alumno):
    nombre = alumno.get("nombre", alumno.get("Usuario", "")).casefold()
    return nombre, alumno["id"]


def clave_orden_historial(fila):
    fecha = fila.get("iniciada_en")
    if not fecha:
        fecha = datetime.min.replace(tzinfo=timezone.utc)
    return fecha


def usuario_puede_acceder_clase(usuario, clase):
    usuario_id = session.get("usuario_id")
    rol = usuario.get("tipo_usuario")
    if rol == "profesor":
        return clase.get("profesor_id") == usuario_id
    if rol == "alumno":
        return usuario_id in clase.get("alumnos_ids", [])
    return False


# Aquí cargamos las publicaciones y respuestas de una clase para mostrarlas juntas.
def cargar_comentarios_clase(clase_ref):
    comentarios = []
    for snapshot in clase_ref.collection("comentarios").order_by("creado_en").stream():
        comentario = snapshot.to_dict() or {}
        comentario["id"] = snapshot.id
        comentario["respuestas"] = []
        for respuesta_snapshot in snapshot.reference.collection("respuestas").order_by("creado_en").stream():
            respuesta = respuesta_snapshot.to_dict() or {}
            respuesta["id"] = respuesta_snapshot.id
            comentario["respuestas"].append(respuesta)
        comentarios.append(comentario)
    return comentarios


def nombre_autor(usuario, nombre_predeterminado):
    nombre = " ".join(filter(None, (usuario.get("nombre"), usuario.get("apellidos"))))
    return nombre.strip() or usuario.get("Usuario", nombre_predeterminado)


def normalizar_telefono_mx(valor):
    texto = (valor or "").strip()
    if not texto:
        return None

    digitos = ""
    for caracter in texto:
        if caracter not in "+0123456789().-" and not caracter.isspace():
            return None
        if caracter in "0123456789":
            digitos += caracter

    if texto.count("+") > 1 or ("+" in texto and not texto.startswith("+")):
        return None

    if len(digitos) == 12 and digitos.startswith("52"):
        digitos = digitos[2:]

    if len(digitos) != 10 or digitos[0] not in "23456789":
        return None

    return digitos


# Este formateo oculta el teléfono completo cuando lo enseñamos en el perfil.
def telefono_enmascarado(valor):
    digitos = ""
    for caracter in valor or "":
        if caracter in "0123456789":
            digitos += caracter
    if not digitos:
        return "Sin teléfono registrado"
    return digitos[:2] + ("*" * max(0, len(digitos) - 2))


# Aquí guardamos una imagen pequeña como dato para mostrarla en el perfil.
def procesar_foto_perfil(foto, max_bytes=700 * 1024):
    if not foto or not foto.filename:
        return None, None

    extension = os.path.splitext(foto.filename)[1].lower()
    formatos = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }
    if extension not in formatos:
        return None, "La imagen debe ser JPG, PNG o WEBP."

    datos = foto.read(max_bytes + 1)
    if len(datos) > max_bytes:
        return None, f"Sube una imagen JPG, PNG o WEBP válida de máximo {max_bytes // 1024} KB."

    mime = formatos[extension]
    return f"data:{mime};base64,{base64.b64encode(datos).decode('ascii')}", None


def existe_otro_documento(documentos, usuario_id):
    return any(documento.id != usuario_id for documento in documentos)


def usuario_valido(nombre_usuario):
    caracteres_permitidos = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-"
    return 4 <= len(nombre_usuario) <= 30 and all(
        caracter in caracteres_permitidos for caracter in nombre_usuario
    )


def codigo_clase_valido(codigo):
    caracteres_permitidos = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"
    return 6 <= len(codigo) <= 16 and all(
        caracter in caracteres_permitidos for caracter in codigo
    )


def fila_id_valido(fila_id):
    caracteres_permitidos = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
    return 1 <= len(fila_id) <= 150 and all(
        caracter in caracteres_permitidos for caracter in fila_id
    )


# Este bloque revisa quién inició sesión y si puede entrar a cada ruta protegida.
def usuario_actual():
    usuario_id = session.get("usuario_id")

    if not usuario_id:
        return None

    documento = db.collection(COLECCION_USUARIOS).document(usuario_id).get()

    if not documento.exists:
        session.clear()
        return None

    return documento.to_dict()


def requiere_login():
    usuario = usuario_actual()

    if not usuario:
        flash("Debes iniciar sesión para continuar.", "error")
        return None

    return usuario


# Rutas de entrada: portada, selección de rol y alta de la cuenta.
@app.route("/")
def index():
    if session.get("usuario_id"):
        usuario = usuario_actual()

        if usuario:
            if usuario.get("tipo_usuario") == "profesor":
                return redirect(url_for("panel_profesor"))

            return redirect(url_for("panel_alumno"))

    return render_template("index.html")


@app.route("/registro", methods=["GET"])
def registro():
    session.pop("tipo_registro", None)
    return render_template("RegistroTipo.html")


@app.route("/seleccionar_tipo", methods=["POST"])
def seleccionar_tipo():
    tipo = request.form.get("tipo_usuario", "").strip().lower()

    if tipo not in ("alumno", "profesor"):
        flash("Selecciona si eres alumno o profesor.", "error")
        return redirect(url_for("registro"))

    if tipo == "profesor":
        codigo = request.form.get("codigo_admin", "")

        if not codigo or codigo != CODIGO_ADMIN:
            flash("El código de administrador es incorrecto.", "error")
            return redirect(url_for("registro"))

    session["tipo_registro"] = tipo

    return redirect(url_for("registro_datos"))


@app.route("/registro/datos", methods=["GET"])
def registro_datos():
    tipo = session.get("tipo_registro")

    if tipo not in ("alumno", "profesor"):
        return redirect(url_for("registro"))

    return render_template("registro.html", tipo_usuario=tipo)


@app.route("/registrar_usuario", methods=["POST"])
def registrar_usuario():
    tipo = session.get("tipo_registro")

    if tipo not in ("alumno", "profesor"):
        flash("Primero selecciona el tipo de cuenta.", "error")
        return redirect(url_for("registro"))

    usuario = request.form.get("Usuario", "").strip()
    clave = request.form.get("Clave", "")
    nombre = request.form.get("nombre", "").strip()
    apellidos = request.form.get("apellidos", "").strip()
    telefono = normalizar_telefono_mx(request.form.get("telefono", ""))

    if not usuario or not clave or not nombre or not apellidos or not telefono:
        if request.form.get("telefono", "").strip() and not telefono:
            flash("Ingresa un teléfono mexicano válido de 10 dígitos, con o sin el prefijo +52.", "error")
            return redirect(url_for("registro_datos"))
        flash("Completa todos los campos obligatorios.", "error")
        return redirect(url_for("registro_datos"))

    if len(nombre) > 50 or len(apellidos) > 80:
        flash("El nombre no puede exceder 50 caracteres y los apellidos 80.", "error")
        return redirect(url_for("registro_datos"))

    if not usuario_valido(usuario):
        flash("El usuario solo puede contener letras, números, puntos, guiones y guiones bajos.", "error")
        return redirect(url_for("registro_datos"))

    usuarios_ref = db.collection(COLECCION_USUARIOS)

    existentes = usuarios_ref.where("Usuario", "==", usuario).limit(1).get()

    if existentes:
        flash("Ese nombre de usuario ya está registrado.", "error")
        return redirect(url_for("registro_datos"))

    telefono_existente = usuarios_ref.where("telefono", "==", telefono).limit(1).get()
    if telefono_existente:
        flash("No pudimos completar el registro. Revisa los datos ingresados e inténtalo de nuevo.", "error")
        return redirect(url_for("registro_datos"))

    nuevo_usuario = {
        "Usuario": usuario,
        "telefono": telefono,
        "password_hash": generate_password_hash(clave),
        "nombre": nombre,
        "apellidos": apellidos,
        "tipo_usuario": tipo,
        "foto_perfil": "",
        "fecha_registro": datetime.now(),
        "activo": True
    }

    if tipo == "alumno":
        nuevo_usuario["matricula"] = request.form.get("matricula", "").strip()
        nuevo_usuario["grado"] = request.form.get("grado", "").strip()
        nuevo_usuario["grupo"] = request.form.get("grupo", "").strip()
        nuevo_usuario["especialidad"] = request.form.get("especialidad", "").strip()

    usuarios_ref.add(nuevo_usuario)

    session.pop("tipo_registro", None)

    flash("Cuenta creada correctamente. Ya puedes iniciar sesión.", "success")
    return redirect(url_for("login"))


# En este bloque se inicia sesión y se valida la contraseña de la cuenta.
@app.route("/login", methods=["GET"])
def login():
    if session.get("usuario_id"):
        usuario = usuario_actual()

        if usuario:
            if usuario.get("tipo_usuario") == "profesor":
                return redirect(url_for("panel_profesor"))

            return redirect(url_for("panel_alumno"))

    return render_template("login.html")


@app.route("/validar_login", methods=["POST"])
def validar_login():
    usuario = request.form.get("Usuario", "").strip()
    clave = request.form.get("Clave", "")

    if not usuario or not clave:
        flash("Debes ingresar usuario y contraseña.", "error")
        return redirect(url_for("login"))

    usuarios = (
        db.collection(COLECCION_USUARIOS)
        .where("Usuario", "==", usuario)
        .limit(1)
        .get()
    )

    datos_usuario = None
    usuario_id = None
    for documento in usuarios:
        datos_usuario = documento.to_dict()
        usuario_id = documento.id
        break

    if not datos_usuario or not datos_usuario.get("activo", True):
        flash("Usuario o contraseña incorrectos.", "error")
        return redirect(url_for("login"))

    hash_guardado = datos_usuario.get("password_hash", "")
    clave_valida = False
    if hash_guardado:
        try:
            clave_valida = check_password_hash(hash_guardado, clave)
        except (ValueError, TypeError):
            clave_valida = False
    else:
        # Esto permite que cuentas antiguas sigan entrando una vez y guarda su clave con hash.
        clave_antigua = datos_usuario.get("Clave") or datos_usuario.get("clave")
        if clave_antigua == clave:
            clave_valida = True
            db.collection(COLECCION_USUARIOS).document(usuario_id).update({
                "password_hash": generate_password_hash(clave),
                "Clave": firestore.DELETE_FIELD,
                "clave": firestore.DELETE_FIELD,
            })

    if not clave_valida:
        flash("Usuario o contraseña incorrectos.", "error")
        return redirect(url_for("login"))

    session.clear()
    session["usuario_id"] = usuario_id
    session["usuario"] = datos_usuario["Usuario"]
    session["tipo_usuario"] = datos_usuario["tipo_usuario"]

    flash("Inicio de sesión correcto.", "success")

    if datos_usuario["tipo_usuario"] == "profesor":
        return redirect(url_for("panel_profesor"))

    return redirect(url_for("panel_alumno"))


# Pantallas del alumno: panel y datos personales.
@app.route("/alumno")
def panel_alumno():
    usuario = requiere_login()

    if not usuario:
        return redirect(url_for("login"))

    if usuario.get("tipo_usuario") != "alumno":
        flash("No tienes permiso para acceder a esa sección.", "error")
        return redirect(url_for("panel_profesor"))

    clases = clases_de_usuario("alumno")
    return render_template("alumno.html", usuario=usuario, clases=clases)


@app.route("/alumno/perfil", methods=["GET"])
def perfil_alumno():
    usuario = requiere_login()

    if not usuario:
        return redirect(url_for("login"))

    if usuario.get("tipo_usuario") != "alumno":
        flash("No tienes permiso para acceder a esa sección.", "error")
        return redirect(url_for("panel_profesor"))

    return render_template(
        "PerfilAlumno.html",
        usuario=usuario,
        telefono_mostrado=telefono_enmascarado(usuario.get("telefono", "")),
    )


def actualizar_perfil_usuario(rol):
    perfil = "perfil_alumno" if rol == "alumno" else "perfil_profesor"
    panel_alternativo = "panel_profesor" if rol == "alumno" else "panel_alumno"
    usuario = requiere_login()
    if not usuario:
        return redirect(url_for("login"))
    if usuario.get("tipo_usuario") != rol:
        flash("No tienes permiso para realizar esta acción.", "error")
        return redirect(url_for(panel_alternativo))

    usuario_id = session["usuario_id"]
    nombre_usuario = request.form.get("Usuario", "").strip()
    nombre = request.form.get("nombre", "").strip()
    apellidos = request.form.get("apellidos", "").strip()

    if not nombre_usuario or not nombre or not apellidos:
        flash("Completa tu usuario, nombre y apellidos.", "error")
        return redirect(url_for(perfil))

    if not usuario_valido(nombre_usuario):
        flash(
            "El usuario debe tener entre 4 y 30 caracteres y solo puede incluir "
            "letras, números, punto, guion o guion bajo.",
            "error",
        )
        return redirect(url_for(perfil))

    if len(nombre) > 50 or len(apellidos) > 80:
        flash("El nombre no puede exceder 50 caracteres y los apellidos 80.", "error")
        return redirect(url_for(perfil))

    usuarios_ref = db.collection(COLECCION_USUARIOS)
    coincidencias = usuarios_ref.where("Usuario", "==", nombre_usuario).limit(2).get()
    if existe_otro_documento(coincidencias, usuario_id):
        flash("Ese nombre de usuario ya está registrado.", "error")
        return redirect(url_for(perfil))

    actualizaciones = {"Usuario": nombre_usuario, "nombre": nombre, "apellidos": apellidos}
    if rol == "alumno":
        for campo in ("matricula", "grado", "grupo", "especialidad"):
            if campo in request.form:
                actualizaciones[campo] = request.form.get(campo, "").strip()

    foto_data_uri, error_foto = procesar_foto_perfil(request.files.get("foto_perfil"))
    if error_foto:
        flash(error_foto, "error")
        return redirect(url_for(perfil))
    if foto_data_uri:
        actualizaciones["foto_perfil"] = foto_data_uri

    usuarios_ref.document(usuario_id).update(actualizaciones)
    session["usuario"] = nombre_usuario
    flash("Tu perfil se actualizó correctamente.", "success")
    return redirect(url_for(perfil))


@app.route("/alumno/perfil/actualizar", methods=["POST"])
def actualizar_perfil_alumno():
    return actualizar_perfil_usuario("alumno")


# Pantallas del profesor: panel, perfil y gestión de sus clases.
@app.route("/profesor")
def panel_profesor():
    usuario = requiere_login()

    if not usuario:
        return redirect(url_for("login"))

    if usuario.get("tipo_usuario") != "profesor":
        flash("No tienes permiso para acceder a esa sección.", "error")
        return redirect(url_for("panel_alumno"))

    clases = clases_de_usuario("profesor")
    return render_template("profesor.html", usuario=usuario, clases=clases)


@app.route("/profesor/perfil", methods=["GET"])
def perfil_profesor():
    usuario = requiere_login()
    if not usuario:
        return redirect(url_for("login"))
    if usuario.get("tipo_usuario") != "profesor":
        flash("No tienes permiso para acceder a esa sección.", "error")
        return redirect(url_for("panel_alumno"))

    return render_template(
        "PerfilProfesor.html",
        usuario=usuario,
        telefono_mostrado=telefono_enmascarado(usuario.get("telefono", "")),
    )


@app.route("/profesor/perfil/actualizar", methods=["POST"])
def actualizar_perfil_profesor():
    return actualizar_perfil_usuario("profesor")


# Este chequeo evita que un alumno use una ruta que está reservada al profesor.
def validar_rol(usuario, rol, endpoint_alternativo):
    if usuario.get("tipo_usuario") != rol:
        flash("No tienes permiso para acceder a esa sección.", "error")
        return redirect(url_for(endpoint_alternativo))
    return None


# El alta de clase valida el código y deja al profesor como responsable de ella.
@app.route("/profesor/clases/crear", methods=["GET", "POST"])
def crear_clase():
    usuario = requiere_login()
    if not usuario:
        return redirect(url_for("login"))
    denegado = validar_rol(usuario, "profesor", "panel_alumno")
    if denegado:
        return denegado
    if request.method == "GET":
        return render_template("crearclase.html", usuario=usuario)

    nombre = request.form.get("nombre", "").strip()
    materia = request.form.get("materia", "").strip()
    codigo = "".join(request.form.get("codigo", "").split()).upper()
    if not nombre or not materia or not codigo:
        flash("Completa el nombre, la materia y el código de clase.", "error")
        return redirect(url_for("crear_clase"))
    if len(nombre) > 80 or len(materia) > 80:
        flash("El nombre y la materia no pueden exceder 80 caracteres.", "error")
        return redirect(url_for("crear_clase"))
    if not codigo_clase_valido(codigo):
        flash("El código debe tener entre 6 y 16 caracteres: letras, números o guiones.", "error")
        return redirect(url_for("crear_clase"))

    clase_ref = db.collection(COLECCION_CLASES).document()
    codigo_ref = db.collection(COLECCION_CODIGOS_CLASES).document(codigo)
    clase = {
        "nombre": nombre,
        "materia": materia,
        "codigo": codigo,
        "profesor_id": session["usuario_id"],
        "profesor_nombre": usuario.get("nombre", usuario.get("Usuario", "Profesor")),
        "profesor_usuario": usuario.get("Usuario", ""),
        "alumnos_ids": [],
        "creada_en": datetime.now(timezone.utc),
    }

    codigo_snapshot = codigo_ref.get()
    if codigo_snapshot.exists:
        flash("Ese código ya está en uso. Elige otro para que tus alumnos puedan identificar la clase.", "error")
        return redirect(url_for("crear_clase"))

    clase_ref.set(clase)
    codigo_ref.set({"clase_id": clase_ref.id, "creado_en": datetime.now(timezone.utc)})
    flash("La clase se creó correctamente.", "success")
    return redirect(url_for("detalle_clase_profesor", clase_id=clase_ref.id))


@app.route("/profesor/clases/<clase_id>")
def detalle_clase_profesor(clase_id):
    usuario = requiere_login()
    if not usuario:
        return redirect(url_for("login"))
    denegado = validar_rol(usuario, "profesor", "panel_alumno")
    if denegado:
        return denegado
    clase = obtener_clase(clase_id)
    if not clase or clase.get("profesor_id") != session["usuario_id"]:
        flash("No se encontró esa clase o no tienes acceso.", "error")
        return redirect(url_for("panel_profesor"))
    fila_snapshot = obtener_fila_actual(db.collection(COLECCION_CLASES).document(clase_id))
    if fila_snapshot:
        fila = fila_snapshot.to_dict()
        fila["id"] = fila_snapshot.id
    else:
        fila = None
    alumnos = obtener_alumnos_clase(clase)
    # La fila antigua guarda nombres e IDs; cargamos la foto actual del perfil
    # aquí para que también aparezca en filas que se crearon antes del cambio.
    if fila and fila.get("alumnos"):
        alumnos_por_id = {alumno["id"]: alumno for alumno in alumnos}
        fila["alumnos"] = [
            {
                **persona,
                "foto_perfil": alumnos_por_id.get(persona.get("alumno_id"), {}).get("foto_perfil", ""),
            }
            for persona in fila["alumnos"]
        ]
    return render_template(
        "claseprofesor.html", usuario=usuario, clase=clase,
        alumnos=alumnos, fila=fila,
    )


@app.route("/profesor/clases/<clase_id>/editar", methods=["GET", "POST"])
def editar_clase(clase_id):
    usuario = requiere_login()
    if not usuario:
        return redirect(url_for("login"))
    denegado = validar_rol(usuario, "profesor", "panel_alumno")
    if denegado:
        return denegado

    clase_ref = db.collection(COLECCION_CLASES).document(clase_id)
    clase = obtener_clase(clase_id)
    if not clase or clase.get("profesor_id") != session["usuario_id"]:
        flash("No se encontró esa clase o no tienes permiso para editarla.", "error")
        return redirect(url_for("panel_profesor"))

    if request.method == "GET":
        return render_template("EditClas.html", usuario=usuario, clase=clase)

    nombre = request.form.get("nombre", "").strip()
    materia = request.form.get("materia", "").strip()
    codigo = "".join(request.form.get("codigo", "").split()).upper()
    if not nombre or not materia or not codigo:
        flash("Completa el nombre, la materia y el código de clase.", "error")
        return redirect(url_for("editar_clase", clase_id=clase_id))
    if len(nombre) > 80 or len(materia) > 80:
        flash("El nombre y la materia no pueden exceder 80 caracteres.", "error")
        return redirect(url_for("editar_clase", clase_id=clase_id))
    if not codigo_clase_valido(codigo):
        flash("El código debe tener entre 6 y 16 caracteres: letras, números o guiones.", "error")
        return redirect(url_for("editar_clase", clase_id=clase_id))

    foto_data_uri = None
    foto = request.files.get("foto_clase")
    if foto and foto.filename:
        foto_data_uri, error_foto = procesar_foto_perfil(foto, max_bytes=300 * 1024)
        if error_foto:
            flash(error_foto, "error")
            return redirect(url_for("editar_clase", clase_id=clase_id))

    codigo_actual = clase.get("codigo", "")
    codigo_nuevo_ref = db.collection(COLECCION_CODIGOS_CLASES).document(codigo)
    codigo_snapshot = codigo_nuevo_ref.get()
    if codigo_snapshot.exists:
        datos_codigo = codigo_snapshot.to_dict() or {}
        if datos_codigo.get("clase_id") != clase_id:
            flash("Ese código ya está en uso. Elige otro para que tus alumnos puedan identificar la clase.", "error")
            return redirect(url_for("editar_clase", clase_id=clase_id))

    cambios = {"nombre": nombre, "materia": materia, "codigo": codigo}
    if foto_data_uri:
        cambios["foto_clase"] = foto_data_uri
    elif request.form.get("quitar_foto") == "1":
        cambios["foto_clase"] = ""
    clase_ref.update(cambios)

    if codigo != codigo_actual:
        codigo_nuevo_ref.set({"clase_id": clase_id, "creado_en": datetime.now(timezone.utc)})
        if codigo_actual:
            codigo_anterior_ref = db.collection(COLECCION_CODIGOS_CLASES).document(codigo_actual)
            codigo_anterior = codigo_anterior_ref.get()
            if codigo_anterior.exists:
                datos_codigo_anterior = codigo_anterior.to_dict() or {}
                if datos_codigo_anterior.get("clase_id") == clase_id:
                    codigo_anterior_ref.delete()

    flash("Los datos de la clase se actualizaron correctamente.", "success")
    return redirect(url_for("detalle_clase_profesor", clase_id=clase_id))


# El profesor administra miembros y el ciclo de vida de la fila virtual desde aquí.
@app.route("/profesor/clases/<clase_id>/alumnos/<alumno_id>/eliminar", methods=["POST"])
def eliminar_alumno_clase(clase_id, alumno_id):
    usuario = requiere_login()
    if not usuario:
        return redirect(url_for("login"))
    denegado = validar_rol(usuario, "profesor", "panel_alumno")
    if denegado:
        return denegado
    clase_ref = db.collection(COLECCION_CLASES).document(clase_id)
    clase = obtener_clase(clase_id)
    if not clase or clase.get("profesor_id") != session["usuario_id"]:
        flash("No se encontró esa clase o no tienes acceso.", "error")
        return redirect(url_for("panel_profesor"))
    if alumno_id not in clase.get("alumnos_ids", []):
        flash("Ese alumno ya no pertenece a la clase.", "error")
        return redirect(url_for("detalle_clase_profesor", clase_id=clase_id))
    clase_ref.update({"alumnos_ids": firestore.ArrayRemove([alumno_id])})
    for fila_snapshot in clase_ref.collection("filas").where("estado", "in", ["abierta", "pausada"]).stream():
        fila = fila_snapshot.to_dict() or {}
        presentes = []
        for persona in fila.get("alumnos", []):
            if persona.get("alumno_id") != alumno_id:
                presentes.append(persona)
        fila_snapshot.reference.update({"alumnos": presentes})
    flash("El alumno se eliminó de la clase.", "success")
    return redirect(url_for("detalle_clase_profesor", clase_id=clase_id))


def controlar_fila_profesor(clase_id, accion):
    usuario = requiere_login()
    if not usuario:
        return redirect(url_for("login"))
    denegado = validar_rol(usuario, "profesor", "panel_alumno")
    if denegado:
        return denegado
    clase_ref = db.collection(COLECCION_CLASES).document(clase_id)
    clase = obtener_clase(clase_id)
    if not clase or clase.get("profesor_id") != session["usuario_id"]:
        flash("No se encontró esa clase o no tienes acceso.", "error")
        return redirect(url_for("panel_profesor"))

    fila_ref = clase_ref.collection("filas").document()
    actual = obtener_fila_actual(clase_ref)
    if accion == "iniciar":
        if actual:
            flash("Ya hay una fila abierta o en pausa para esta clase.", "error")
        else:
            ahora = datetime.now(timezone.utc)
            fila_ref.set({
                "estado": "abierta", "iniciada_en": ahora, "finalizada_en": None,
                "alumnos": [], "participantes_registrados": [],
            })
            flash("La fila está abierta. Los alumnos ya pueden unirse.", "success")
    elif not actual:
        flash("No hay una fila activa para esta clase.", "error")
    elif accion == "pausar":
        if actual.to_dict().get("estado") != "abierta":
            flash("La fila no está abierta.", "error")
        else:
            actual.reference.update({"estado": "pausada", "pausada_en": datetime.now(timezone.utc)})
            flash("La fila está en pausa. No se permiten nuevas incorporaciones.", "success")
    elif accion == "reanudar":
        if actual.to_dict().get("estado") != "pausada":
            flash("La fila no está en pausa.", "error")
        else:
            actual.reference.update({"estado": "abierta", "reanudad_en": datetime.now(timezone.utc)})
            flash("La fila volvió a abrirse.", "success")
    elif accion == "finalizar":
        ahora = datetime.now(timezone.utc)
        actual.reference.update({
            "estado": "finalizada", "finalizada_en": ahora,
        })
        flash("La fila se finalizó. Puedes consultarla en el historial de la clase.", "success")
    return redirect(url_for("detalle_clase_profesor", clase_id=clase_id))


@app.route("/profesor/clases/<clase_id>/fila/iniciar", methods=["POST"])
def iniciar_fila(clase_id):
    return controlar_fila_profesor(clase_id, "iniciar")


@app.route("/profesor/clases/<clase_id>/fila/pausar", methods=["POST"])
def pausar_fila(clase_id):
    return controlar_fila_profesor(clase_id, "pausar")


@app.route("/profesor/clases/<clase_id>/fila/reanudar", methods=["POST"])
def reanudar_fila(clase_id):
    return controlar_fila_profesor(clase_id, "reanudar")


@app.route("/profesor/clases/<clase_id>/fila/finalizar", methods=["POST"])
def finalizar_fila(clase_id):
    return controlar_fila_profesor(clase_id, "finalizar")


@app.route("/profesor/clases/<clase_id>/historial")
def historial_filas(clase_id):
    usuario = requiere_login()
    if not usuario:
        return redirect(url_for("login"))
    denegado = validar_rol(usuario, "profesor", "panel_alumno")
    if denegado:
        return denegado
    clase = obtener_clase(clase_id)
    if not clase or clase.get("profesor_id") != session["usuario_id"]:
        flash("No se encontró esa clase o no tienes acceso.", "error")
        return redirect(url_for("panel_profesor"))
    filas = []
    for snapshot in db.collection(COLECCION_CLASES).document(clase_id).collection("filas").stream():
        fila = snapshot.to_dict() or {}
        fila["id"] = snapshot.id
        filas.append(fila)
    filas.sort(key=clave_orden_historial, reverse=True)
    return render_template("historialfilas.html", usuario=usuario, clase=clase, filas=filas)


# Aquí el alumno usa un código para entrar a una clase y después puede abrirla.
@app.route("/alumno/clases/unirse", methods=["POST"])
def unirse_a_clase():
    usuario = requiere_login()
    if not usuario:
        return redirect(url_for("login"))
    denegado = validar_rol(usuario, "alumno", "panel_profesor")
    if denegado:
        return denegado
    codigo = "".join(request.form.get("codigo", "").split()).upper()
    if not codigo_clase_valido(codigo):
        flash("Ingresa un código de clase válido.", "error")
        return redirect(url_for("panel_alumno"))
    codigo_snapshot = None
    if codigo:
        codigo_snapshot = db.collection(COLECCION_CODIGOS_CLASES).document(codigo).get()
    if not codigo_snapshot or not codigo_snapshot.exists:
        flash("No encontramos una clase con ese código.", "error")
        return redirect(url_for("panel_alumno"))
    clase_id = (codigo_snapshot.to_dict() or {}).get("clase_id")
    clase_ref = db.collection(COLECCION_CLASES).document(clase_id)
    clase = obtener_clase(clase_id)
    if not clase:
        flash("No encontramos una clase con ese código.", "error")
        return redirect(url_for("panel_alumno"))
    if session["usuario_id"] in clase.get("alumnos_ids", []):
        flash("Ya formas parte de esa clase.", "success")
        return redirect(url_for("panel_alumno"))
    clase_ref.update({"alumnos_ids": firestore.ArrayUnion([session["usuario_id"]])})
    flash(f"Te uniste a {clase.get('nombre', 'la clase')}.", "success")
    return redirect(url_for("panel_alumno"))


@app.route("/alumno/clases/<clase_id>")
def detalle_clase_alumno(clase_id):
    usuario = requiere_login()
    if not usuario:
        return redirect(url_for("login"))
    denegado = validar_rol(usuario, "alumno", "panel_profesor")
    if denegado:
        return denegado
    clase = obtener_clase(clase_id)
    if not clase or session["usuario_id"] not in clase.get("alumnos_ids", []):
        flash("No perteneces a esa clase o no tienes acceso.", "error")
        return redirect(url_for("panel_alumno"))
    fila_snapshot = obtener_fila_actual(db.collection(COLECCION_CLASES).document(clase_id))
    if fila_snapshot:
        fila = fila_snapshot.to_dict()
        fila["id"] = fila_snapshot.id
    else:
        fila = None
    return render_template("clasealumno.html", usuario=usuario, clase=clase, fila=fila)


# Estas rutas cargan la conversación de la clase y guardan publicaciones/respuestas.
@app.route("/clases/<clase_id>/comentarios")
def comentarios_clase(clase_id):
    usuario = requiere_login()
    if not usuario:
        return redirect(url_for("login"))

    clase = obtener_clase(clase_id)
    if not clase or not usuario_puede_acceder_clase(usuario, clase):
        flash("No tienes acceso a las conversaciones de esa clase.", "error")
        if usuario.get("tipo_usuario") == "profesor":
            destino = "panel_profesor"
        else:
            destino = "panel_alumno"
        return redirect(url_for(destino))

    clase_ref = db.collection(COLECCION_CLASES).document(clase_id)
    comentarios = cargar_comentarios_clase(clase_ref)
    return render_template(
        "Coments.html", usuario=usuario, clase=clase, comentarios=comentarios,
    )


@app.route("/clases/<clase_id>/comentarios/publicar", methods=["POST"])
def publicar_comentario(clase_id):
    usuario = requiere_login()
    if not usuario:
        return redirect(url_for("login"))
    clase = obtener_clase(clase_id)
    if not clase or not usuario_puede_acceder_clase(usuario, clase):
        flash("No tienes acceso a esa clase.", "error")
        if usuario.get("tipo_usuario") == "profesor":
            destino = "panel_profesor"
        else:
            destino = "panel_alumno"
        return redirect(url_for(destino))
    if usuario.get("tipo_usuario") != "profesor":
        flash("Solo el profesor puede iniciar una publicación de clase.", "error")
        return redirect(url_for("comentarios_clase", clase_id=clase_id))

    contenido = request.form.get("contenido", "").strip()
    if not contenido or len(contenido) > 3000:
        flash("Escribe un mensaje de hasta 3000 caracteres.", "error")
        return redirect(url_for("comentarios_clase", clase_id=clase_id))

    autor_nombre = nombre_autor(usuario, "Profesor")
    db.collection(COLECCION_CLASES).document(clase_id).collection("comentarios").add({
        "contenido": contenido,
        "autor_id": session["usuario_id"],
        "autor_nombre": autor_nombre,
        "autor_usuario": usuario.get("Usuario", ""),
        "autor_rol": "profesor",
        "autor_foto": usuario.get("foto_perfil", ""),
        "creado_en": datetime.now(timezone.utc),
    })
    flash("Tu publicación quedó visible para la clase.", "success")
    return redirect(url_for("comentarios_clase", clase_id=clase_id))


@app.route("/clases/<clase_id>/comentarios/<comentario_id>/responder", methods=["POST"])
def responder_comentario(clase_id, comentario_id):
    usuario = requiere_login()
    if not usuario:
        return redirect(url_for("login"))
    clase = obtener_clase(clase_id)
    if not clase or not usuario_puede_acceder_clase(usuario, clase):
        flash("No tienes acceso a esa clase.", "error")
        if usuario.get("tipo_usuario") == "profesor":
            destino = "panel_profesor"
        else:
            destino = "panel_alumno"
        return redirect(url_for(destino))

    clase_ref = db.collection(COLECCION_CLASES).document(clase_id)
    comentario_ref = clase_ref.collection("comentarios").document(comentario_id)
    if not comentario_ref.get().exists:
        flash("No encontramos esa publicación.", "error")
        return redirect(url_for("comentarios_clase", clase_id=clase_id))

    contenido = request.form.get("contenido", "").strip()
    if not contenido or len(contenido) > 2000:
        flash("Escribe una respuesta de hasta 2000 caracteres.", "error")
        return redirect(url_for("comentarios_clase", clase_id=clase_id))

    rol = usuario.get("tipo_usuario", "alumno")
    autor_nombre = nombre_autor(usuario, "Usuario")
    comentario_ref.collection("respuestas").add({
        "contenido": contenido,
        "autor_id": session["usuario_id"],
        "autor_nombre": autor_nombre,
        "autor_usuario": usuario.get("Usuario", ""),
        "autor_rol": rol,
        "autor_foto": usuario.get("foto_perfil", ""),
        "creado_en": datetime.now(timezone.utc),
    })
    flash("Tu respuesta se agregó a la conversación.", "success")
    return redirect(url_for("comentarios_clase", clase_id=clase_id) + f"#comentario-{comentario_id}")


@app.route("/clases/<clase_id>/comentarios/<comentario_id>/eliminar", methods=["POST"])
def eliminar_comentario(clase_id, comentario_id):
    usuario = requiere_login()
    if not usuario:
        return redirect(url_for("login"))

    clase = obtener_clase(clase_id)
    if not clase or not usuario_puede_acceder_clase(usuario, clase):
        flash("No tienes acceso a esa clase.", "error")
        destino = "panel_profesor" if usuario.get("tipo_usuario") == "profesor" else "panel_alumno"
        return redirect(url_for(destino))

    comentario_ref = db.collection(COLECCION_CLASES).document(clase_id).collection("comentarios").document(comentario_id)
    comentario_snapshot = comentario_ref.get()
    comentario = comentario_snapshot.to_dict() or {}
    if not comentario_snapshot.exists:
        flash("No encontramos ese comentario.", "error")
    elif comentario.get("autor_id") != session.get("usuario_id"):
        flash("Solo puedes borrar tus propios comentarios.", "error")
    else:
        # Firestore no elimina automáticamente las subcolecciones del comentario.
        for respuesta_snapshot in comentario_ref.collection("respuestas").stream():
            respuesta_snapshot.reference.delete()
        comentario_ref.delete()
        flash("Tu comentario y sus respuestas se eliminaron.", "success")
    return redirect(url_for("comentarios_clase", clase_id=clase_id))


@app.route("/clases/<clase_id>/comentarios/<comentario_id>/respuestas/<respuesta_id>/eliminar", methods=["POST"])
def eliminar_respuesta_comentario(clase_id, comentario_id, respuesta_id):
    usuario = requiere_login()
    if not usuario:
        return redirect(url_for("login"))

    clase = obtener_clase(clase_id)
    if not clase or not usuario_puede_acceder_clase(usuario, clase):
        flash("No tienes acceso a esa clase.", "error")
        destino = "panel_profesor" if usuario.get("tipo_usuario") == "profesor" else "panel_alumno"
        return redirect(url_for(destino))

    comentario_ref = db.collection(COLECCION_CLASES).document(clase_id).collection("comentarios").document(comentario_id)
    respuesta_ref = comentario_ref.collection("respuestas").document(respuesta_id)
    respuesta_snapshot = respuesta_ref.get()
    respuesta = respuesta_snapshot.to_dict() or {}
    if not respuesta_snapshot.exists:
        flash("No encontramos esa respuesta.", "error")
    elif respuesta.get("autor_id") != session.get("usuario_id"):
        flash("Solo puedes borrar tus propias respuestas.", "error")
    else:
        respuesta_ref.delete()
        flash("Tu respuesta se eliminó.", "success")
    return redirect(url_for("comentarios_clase", clase_id=clase_id) + f"#comentario-{comentario_id}")


# El alumno se anota en la fila solo si sigue abierta y pertenece a esa clase.
@app.route("/alumno/clases/<clase_id>/fila/unirse", methods=["POST"])
def unirse_a_fila(clase_id):
    usuario = requiere_login()
    if not usuario:
        return redirect(url_for("login"))
    denegado = validar_rol(usuario, "alumno", "panel_profesor")
    if denegado:
        return denegado
    clase_ref = db.collection(COLECCION_CLASES).document(clase_id)
    fila_id = request.form.get("fila_id", "")
    if not fila_id_valido(fila_id):
        flash("La fila seleccionada no es válida.", "error")
        return redirect(url_for("detalle_clase_alumno", clase_id=clase_id))
    fila_ref = clase_ref.collection("filas").document(fila_id)
    clase_snapshot = clase_ref.get()
    fila_snapshot = fila_ref.get()
    resultado = "agregado"

    if not clase_snapshot.exists or not fila_snapshot.exists:
        resultado = "no_encontrada"
    else:
        clase_datos = clase_snapshot.to_dict() or {}
        if (
            clase_datos.get("profesor_id") == session["usuario_id"]
            or session["usuario_id"] not in clase_datos.get("alumnos_ids", [])
        ):
            resultado = "sin_acceso"
        else:
            fila_datos = fila_snapshot.to_dict() or {}
            if fila_datos.get("estado") != "abierta":
                resultado = "cerrada"
            else:
                alumnos = list(fila_datos.get("alumnos", []))
                participantes_registrados = list(fila_datos.get("participantes_registrados", alumnos))
                for persona in participantes_registrados:
                    if persona.get("alumno_id") == session["usuario_id"]:
                        resultado = "ya_esta"
                        break

                if resultado == "agregado":
                    participante = {
                        "alumno_id": session["usuario_id"],
                        "nombre": usuario.get("nombre", usuario.get("Usuario", "Alumno")),
                        "apellidos": usuario.get("apellidos", ""),
                        "Usuario": usuario.get("Usuario", ""),
                        "unido_en": datetime.now(timezone.utc),
                    }
                    alumnos.append(participante)
                    participantes_registrados.append(participante)
                    fila_ref.update({
                        "alumnos": alumnos,
                        "participantes_registrados": participantes_registrados,
                    })
    mensajes = {
        "no_encontrada": ("La clase o la fila ya no está disponible.", "error"),
        "sin_acceso": ("No tienes acceso a esa fila.", "error"),
        "cerrada": ("La fila está en pausa o ya finalizó.", "error"),
        "ya_esta": ("Ya estás en esta fila.", "success"),
        "agregado": ("Te uniste a la fila.", "success"),
    }
    mensaje, categoria = mensajes[resultado]
    flash(mensaje, categoria)
    return redirect(url_for("detalle_clase_alumno", clase_id=clase_id))


@app.route("/logout")
def logout():
    session.clear()
    flash("Sesión cerrada correctamente.", "success")
    return redirect(url_for("index"))
if __name__ == "__main__":
    app.run(debug=True, port=5000)
