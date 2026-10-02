#!/usr/bin/env python3
"""
Obtiene de EMPIAR/EMDB los parámetros necesarios para lanzar el workflow
de Scipion "2D Streaming" (Xmipp):
https://workflowhub.eu/workflows/2169

    scipion3 template workflow_2D_xmipp.json.template \
        moviespath='...' filepatern='...' sa='...' ac='...' sr='...' \
        dose='...' gain='...' gainRot='...' gainFlip='...'

Fuentes de datos:
  - EMPIAR REST API  (https://www.ebi.ac.uk/empiar/api/entry/<id>/)
      -> imageset de movies: directory, pixel size (sr)
  - Directorio local descargado o EMPIAR FTP/HTTPS listing
    (https://ftp.ebi.ac.uk/empiar/world_availability/<id>/)
      -> localización real de los ficheros (la estructura de "directory" que
         devuelve la API no siempre coincide 1:1 con el árbol FTP, así que se
         verifica), búsqueda del fichero de gain y detección de la extensión
         real de las movies para filepatern.
  - EMDB REST API (https://www.ebi.ac.uk/emdb/api/entry/<EMD-id>), a través de
    la cross-reference EMD-XXXX de la entrada EMPIAR
      -> spherical aberration (nominal_cs) y dosis por frame.

EMPIAR/EMDB no publican "amplitude contrast" (ac); se usa siempre el valor
por defecto habitual en crio-EM (0.1). Cuando cualquier otro dato no se
puede obtener, se rellena con el valor por defecto correspondiente.

Uso:
    python3 scipion_EMPIAR.py 10352
    python3 scipion_EMPIAR.py EMPIAR-10352 --json
    python3 scipion_EMPIAR.py 10352 --template workflow.json
"""

import argparse
import json
import os
import re
import sys
import subprocess
from collections import Counter

import requests

EMPIAR_API = "https://www.ebi.ac.uk/empiar/api/entry/{id}/"
EMDB_API = "https://www.ebi.ac.uk/emdb/api/entry/{emd_id}"
EMPIAR_FTP_ROOT = "https://ftp.ebi.ac.uk/empiar/world_availability/{id}"

TIMEOUT = 30

DEFAULTS = {
    "sa": 2.7,     # spherical aberration (mm)
    "ac": 0.1,     # amplitude contrast (no se publica en EMPIAR/EMDB)
    "sr": 1.0,     # sampling rate / pixel size (A/px)
    "dose": 1.0,   # dosis por frame (e/A^2)
}

MOVIE_CATEGORY_PRIORITY = [
    "micrographs - multiframe",
    "micrographs - multi-frame",
]

GAIN_KEYWORDS = [
    "gain", "dark", "norm",
    # Convención Gatan/SerialEM para referencias de gain de K2/K3
    # (p.ej. EMPIAR-10305: "SuperRef_TMV_001_Sep18.dm4"), que no contienen
    # ninguna de las palabras anteriores.
    "superref", "super_ref", "super-ref",
    "countref", "count_ref", "count-ref",
]

# Formatos propios de Gatan DigitalMicrograph, usados casi siempre para
# referencias de gain y prácticamente nunca como formato de movie.
GAIN_EXTENSIONS = (".dm3", ".dm4")

MOVIE_EXTENSIONS = (".tif", ".tiff", ".mrc", ".mrcs", ".eer")

# Mejor esfuerzo para cuando no se puede listar la carpeta de movies (p.ej.
# no se ha encontrado el directorio real en el FTP): EMPIAR declara el
# formato como "TIFF"/"MRC"/"EER", pero eso no dice si la extensión real es
# .tif o .tiff — la mayoría de depositantes usan .tif pese a llamarlo "TIFF".
DATA_FORMAT_TO_EXT = {
    "TIFF": "*.tif",
    "MRC": "*.mrcs",
    "EER": "*.eer",
}

HREF_RE = re.compile(r'href="([^"?][^"]*)"')


def clean_id(raw):
    return str(raw).upper().replace("EMPIAR-", "").strip()


def fetch_json(url):
    r = requests.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def get_empiar_entry(empiar_num):
    data = fetch_json(EMPIAR_API.format(id=empiar_num))
    key = f"EMPIAR-{empiar_num}"
    return data.get(key) or data[list(data.keys())[0]]


def is_url(path):
    return str(path).startswith(("http://", "https://"))


def as_dir(path):
    return path.rstrip("/") + "/" if is_url(path) else os.path.join(path, "")


def join_path(base, name):
    return base.rstrip("/") + "/" + name if is_url(base) else os.path.join(base, name.rstrip("/"))


def pick_movie_imageset(imagesets):
    if not imagesets:
        return None
    for wanted in MOVIE_CATEGORY_PRIORITY:
        for im in imagesets:
            if (im.get("category") or "").lower() == wanted:
                return im
    for im in imagesets:
        if "multiframe" in (im.get("category") or "").lower():
            return im
    return imagesets[0]


def dir_exists(path):
    if not is_url(path):
        return os.path.isdir(path)

    try:
        r = requests.head(path, timeout=TIMEOUT, allow_redirects=True)
        return r.status_code == 200
    except requests.RequestException:
        return False


def list_dir(path):
    """Devuelve [(nombre, es_directorio), ...] de un directorio local o listado web."""
    if not is_url(path):
        try:
            entries = []
            with os.scandir(path) as it:
                for entry in it:
                    name = entry.name
                    is_dir = entry.is_dir()
                    entries.append((name + "/" if is_dir else name, is_dir))
            return entries
        except OSError:
            return []

    try:
        r = requests.get(path, timeout=TIMEOUT)
        r.raise_for_status()
    except requests.RequestException:
        return []

    entries = []
    for href in HREF_RE.findall(r.text):
        if href in ("../", "/") or href.startswith("/"):
            # enlaces absolutos == "Parent Directory", no son hijos reales
            continue
        entries.append((href, href.endswith("/")))
    return entries


def bfs_find_movies_dir(root_url, leaf_name, max_nodes=200, max_depth=5):
    """
    Busca en el árbol FTP/local de la entrada un directorio cuyo nombre coincida
    con leaf_name (nombre de carpeta reportado por la API de EMPIAR).
    Necesario porque la API no siempre refleja la ruta FTP real.
    """
    queue = [(as_dir(root_url), 0)]
    visited = set()
    nodes = 0

    while queue and nodes < max_nodes:
        url, depth = queue.pop(0)
        if url in visited or depth > max_depth:
            continue
        visited.add(url)
        nodes += 1

        for name, is_dir in list_dir(url):
            if not is_dir:
                continue
            child = join_path(url, name)
            if name.rstrip("/").lower() == leaf_name.lower():
                return as_dir(child)
            queue.append((child, depth + 1))

    return None


def refine_to_files_dir(url, max_depth=3):
    """
    Algunos depositantes anidan las movies un nivel más adentro de lo que
    indica la API (p.ej. "directory": "data" pero las movies reales están en
    "data/CR-BIS-SPA_Falcon4i/"). Si la carpeta encontrada no tiene ficheros
    y solo contiene una subcarpeta, bajamos a esa subcarpeta.
    """
    for _ in range(max_depth):
        entries = list_dir(url)
        files = [n for n, is_dir in entries if not is_dir]
        dirs = [n for n, is_dir in entries if is_dir]

        if files or len(dirs) != 1:
            return url

        url = as_dir(join_path(url, dirs[0]))

    return url


def local_entry_roots(empiar_num, scipion_user_data=None):
    roots = []
    for base in (".", scipion_user_data):
        if not base:
            continue

        root = os.path.abspath(os.path.join(base, str(empiar_num)))
        if os.path.isdir(root) and root not in roots:
            roots.append(root)

    return roots


def find_movies_directory(empiar_num, directory_hint, scipion_user_data=None):
    """
    Localiza la ruta real de la carpeta de movies.
    Si existe ./<id>/, prueba primero ese árbol local. Si scipion_user_data es
    distinto y contiene <id>/, prueba también ese árbol. Si no encuentra nada,
    usa el FTP y prueba las dos convenciones observadas en EMPIAR:
      - <root>/<directory>/
      - <root>/data/<directory>/   (algunos depositantes anidan "data/data/…")
    y si ninguna existe, recorre el árbol buscando por nombre de carpeta.
    El resultado se refina por si las movies están un nivel más adentro.
    """
    if not directory_hint:
        return None

    directory_hint = directory_hint.strip("/")
    roots = local_entry_roots(empiar_num, scipion_user_data)
    roots.append(EMPIAR_FTP_ROOT.format(id=empiar_num))

    for root in roots:
        candidates = [
            as_dir(join_path(root, directory_hint)),
            as_dir(join_path(join_path(root, "data"), directory_hint)),
        ]

        for candidate in candidates:
            if dir_exists(candidate):
                return refine_to_files_dir(candidate)

        leaf = directory_hint.split("/")[-1]
        found = bfs_find_movies_dir(root, leaf)
        if found:
            return refine_to_files_dir(found)

    return None


def parent_location(path):
    if is_url(path):
        trimmed = path.rstrip("/")
        idx = trimmed.rfind("/")
        return trimmed[: idx + 1]
    return as_dir(os.path.dirname(os.path.normpath(path)))


def is_gain_file(name):
    return any(k in name.lower() for k in GAIN_KEYWORDS) or name.lower().endswith(".gain")


def _ext_of(name):
    idx = name.rfind(".")
    return name[idx:].lower() if idx != -1 else ""


def guess_filepatern(file_names):
    """
    Determina el patrón glob de las movies (para 'filesPattern' de
    ProtImportMovies) a partir de las extensiones realmente presentes en la
    carpeta de movies, en vez de asumir siempre '*.tiff' (el valor por
    defecto de la plantilla) — la propia EMPIAR API declara el formato como
    "TIFF" incluso cuando la extensión real de los ficheros es '.tif', y
    algunos detectores usan '.eer'/'.mrc'.
    """
    counts = Counter()
    for name in file_names:
        ext = _ext_of(name)
        if ext in MOVIE_EXTENSIONS:
            counts[name[-len(ext):]] += 1

    if not counts:
        return None

    best_ext, _ = counts.most_common(1)[0]
    return f"*{best_ext}"


def find_gain_by_extension(file_names):
    """
    Último recurso: sin coincidencia por nombre, busca un único fichero en
    formato Gatan (.dm3/.dm4) que conviva con las movies en otro formato
    (p.ej. .tif) — es habitual que sea la referencia de gain aunque su
    nombre no lo indique (p.ej. EMPIAR-10305: "SuperRef_..._Sep18.dm4").
    Si hay más de un candidato no se puede decidir y se descarta.
    """
    movie_like = [n for n in file_names if _ext_of(n) in MOVIE_EXTENSIONS]
    if not movie_like:
        return None

    candidates = [n for n in file_names if _ext_of(n) in GAIN_EXTENSIONS]
    return candidates[0] if len(candidates) == 1 else None


def _search_gain_in_subdir(dir_url, max_depth):
    """
    Busca un fichero de gain dentro de una subcarpeta cuyo NOMBRE ya sugiere
    que es la carpeta del gain (p.ej. "gainRefer/", visto en EMPIAR-12567,
    donde el gain no está junto a "movies/" sino en una carpeta hermana
    dedicada bajo "data/"). Acotado a max_depth niveles de recursión.
    """
    if max_depth <= 0:
        return None

    entries = list_dir(dir_url)
    files = [(n, d) for n, d in entries if not d]
    dirs = [(n, d) for n, d in entries if d]

    for name, _ in files:
        if is_gain_file(name):
            return dir_url, name

    by_ext = find_gain_by_extension([n for n, _ in files])
    if by_ext:
        return dir_url, by_ext

    # Carpeta dedicada al gain con un único fichero dentro: asumimos que es
    # ese, aunque su nombre no contenga "gain"/"dark"/"norm" literalmente
    # (el nombre de la propia carpeta ya es la señal fuerte aquí).
    if len(files) == 1 and not dirs:
        return dir_url, files[0][0]

    for name, _ in dirs:
        found = _search_gain_in_subdir(as_dir(join_path(dir_url, name)), max_depth - 1)
        if found:
            return found

    return None


def find_gain_file(empiar_num, movies_dir, max_levels_up=2, scipion_user_data=None,
                   max_subdir_depth=2):
    """
    Busca el fichero de gain en la carpeta de movies y, si no está ahí,
    en los directorios padre (hasta max_levels_up niveles, sin salir de la
    carpeta de la propia entrada EMPIAR) — es habitual que el gain se
    deposite junto a "data/" cubriendo varias subcarpetas de movies.
    También busca en subcarpetas cuyo nombre sugiera que contienen el gain.
    """
    if not movies_dir:
        return None, None

    if is_url(movies_dir):
        root = EMPIAR_FTP_ROOT.format(id=empiar_num).rstrip("/") + "/"
    else:
        root = as_dir(os.path.abspath(os.path.join(".", str(empiar_num))))
        movies_abs = os.path.abspath(movies_dir)
        for entry_root in local_entry_roots(empiar_num, scipion_user_data):
            try:
                if os.path.commonpath([movies_abs, entry_root]) == entry_root:
                    root = as_dir(entry_root)
                    break
            except ValueError:
                pass

    url = as_dir(movies_dir)
    visited = set()

    for _ in range(max_levels_up + 1):
        if not url or url in visited:
            break
        visited.add(url)

        entries = list_dir(url)
        file_names = [n for n, d in entries if not d]

        for name, is_dir in entries:
            if not is_dir and is_gain_file(name):
                return url, name

        by_ext = find_gain_by_extension(file_names)
        if by_ext:
            return url, by_ext

        for name, is_dir in entries:
            if is_dir and is_gain_file(name):
                found = _search_gain_in_subdir(as_dir(join_path(url, name)), max_subdir_depth)
                if found:
                    return found

        if url == root:
            break
        url = parent_location(url)

    return None, None


def to_local_moviespath(empiar_num, movies_dir, scipion_user_data=None):
    if not is_url(movies_dir):
        return as_dir(os.path.abspath(movies_dir))

    root = EMPIAR_FTP_ROOT.format(id=empiar_num)
    suffix = movies_dir[len(root):]
    base = os.path.abspath(scipion_user_data or ".")
    return as_dir(os.path.join(base, f"{empiar_num}{suffix}"))


def parse_float(value_of):
    try:
        return float(value_of)
    except (TypeError, ValueError):
        return None


DEGREES_TO_ROT_CODE = {0: 0, 90: 1, 180: 2, 270: 3}

# MotionCor2/3 rotation code para el gain: 0/1/2/3 = 0/90/180/270 grados.
GAIN_ROT_RE = re.compile(
    r"r\s*/\s*f\s*(?P<rot>[0-3])(?:\s*/\s*(?P<flip>[0-2]))?", re.IGNORECASE
)
GAIN_DEGREES_RE = re.compile(
    r"(?P<deg>\d{1,3})\s*(?:°|degrees?)\s*rotation", re.IGNORECASE
)
GAIN_FLIP_UPSIDE_RE = re.compile(r"flip\s+upside\s+down", re.IGNORECASE)
GAIN_FLIP_LEFTRIGHT_RE = re.compile(r"flip\s+left\s*-?\s*right", re.IGNORECASE)


def parse_gain_orientation(text):
    """
    Muchos depositantes de EMPIAR indican en 'details' cómo hay que rotar o
    voltear el gain para que coincida con la orientación de las movies (p.ej.
    EMPIAR-10305: "TIFF files have a 270° rotation with the gain reference
    (r/f 3)"; EMPIAR-10352: "use gain.mrc with 90 degree rotation and flip
    upside down"). Si no se corrige, MotionCor2/3 detecta el mismatch de
    dimensiones y descarta la corrección de movimiento en TODAS las movies
    sin lanzar ningún error visible en el log de Scipion (solo en
    run.stderr), y el protocolo termina en 'failed' pese a mostrar
    "DONE N/N".

    Devuelve (gain_rot, gain_flip) como códigos de MotionCor2/3 (0-3 y 0-2),
    o (None, None) si no se ha encontrado ninguna pista en el texto.
    """
    if not text:
        return None, None

    m = GAIN_ROT_RE.search(text)
    if m:
        rot = int(m.group("rot"))
        flip = int(m.group("flip")) if m.group("flip") is not None else 0
        return rot, flip

    rot = None
    m = GAIN_DEGREES_RE.search(text)
    if m:
        rot = DEGREES_TO_ROT_CODE.get(int(m.group("deg")))

    flip = None
    if GAIN_FLIP_UPSIDE_RE.search(text):
        flip = 1
    elif GAIN_FLIP_LEFTRIGHT_RE.search(text):
        flip = 2

    if rot is None and flip is None:
        return None, None

    return rot or 0, flip or 0


def get_emdb_microscopy(emd_id):
    """
    Mejor esfuerzo: (spherical_aberration_mm, dosis_total_por_imagen, n_frames)
    desde EMDB. La dosis que publica EMDB es la dosis total acumulada por
    imagen/movie, no por frame; hay que dividirla por el número de frames.
    """
    try:
        data = fetch_json(EMDB_API.format(emd_id=emd_id))
        sd_list = data["structure_determination_list"]["structure_determination"]
    except (requests.RequestException, KeyError, ValueError):
        return None, None, None

    sa = None
    total_dose = None
    n_frames = None

    for sd in sd_list:
        for m in sd.get("microscopy_list", {}).get("microscopy", []):

            if sa is None:
                sa = parse_float((m.get("nominal_cs") or {}).get("valueOf_"))

            for ir in m.get("image_recording_list", {}).get("image_recording", []):
                if total_dose is None:
                    total_dose = parse_float(
                        (ir.get("average_electron_dose_per_image") or {}).get("valueOf_")
                    )

                if n_frames is None:
                    frames_raw = (
                        ir.get("digitization_details", {}).get("frames_per_image")
                    )
                    if frames_raw:
                        try:
                            n_frames = int(str(frames_raw).split("-")[-1])
                        except ValueError:
                            n_frames = None

    return sa, total_dose, n_frames


def harvest(empiar_id, scipion_user_data=None):
    num = clean_id(empiar_id)
    entry = get_empiar_entry(num)

    imageset = pick_movie_imageset(entry.get("imagesets", []))
    directory_hint = (imageset or {}).get("directory")

    movies_dir_url = find_movies_directory(num, directory_hint, scipion_user_data)

    if movies_dir_url:
        moviespath = to_local_moviespath(num, movies_dir_url, scipion_user_data)
        gain_dir_url, gain_file = find_gain_file(num, movies_dir_url, scipion_user_data=scipion_user_data)
        gain = (
            f"{to_local_moviespath(num, gain_dir_url, scipion_user_data)}{gain_file}"
            if gain_file
            else ""
        )
        movies_dir_files = [n for n, is_dir in list_dir(movies_dir_url) if not is_dir]
        filepatern = guess_filepatern(movies_dir_files)
    else:
        base = os.path.abspath(scipion_user_data or ".")
        moviespath = f"{base}/{num}/data/{directory_hint}/" if directory_hint else ""
        gain = ""
        filepatern = None

    if not filepatern:
        data_format = (imageset or {}).get("data_format") or (imageset or {}).get("header_format")
        filepatern = DATA_FORMAT_TO_EXT.get((data_format or "").upper())
        print(
            f"[WARNING] EMPIAR-{num}: no se ha podido determinar 'filepatern' "
            f"listando la carpeta de movies; se usa el valor por "
            f"{'defecto según data_format (' + data_format + ')' if filepatern else 'defecto de la plantilla'} "
            f"— revisa que coincida con la extensión real de los ficheros.",
            file=sys.stderr,
        )
        filepatern = filepatern or "*.tiff"

    gain_rot, gain_flip = (None, None)
    if gain:
        gain_rot, gain_flip = parse_gain_orientation((imageset or {}).get("details"))
        if gain_rot is None:
            print(
                f"[WARNING] EMPIAR-{num}: se ha encontrado un gain ({gain}) pero "
                f"no se ha detectado en 'details' ninguna pista sobre su "
                f"orientación (rotación/flip) respecto a las movies. Si el gain "
                f"está traspuesto y no se corrige, MotionCor2/3 saltará la "
                f"corrección de movimiento en TODAS las movies sin dar error "
                f"visible en Scipion, y el protocolo terminará en 'failed' pese "
                f"a completar todos los pasos. Revisa las dimensiones del gain "
                f"contra las de una movie y, si no coinciden, añade "
                f"'gainRot='/'gainFlip=' manualmente (0/1/2/3 = 0/90/180/270 "
                f"grados; flip 0=ninguno/1=upside-down/2=left-right).",
                file=sys.stderr,
            )

    if not gain:
        print(
            f"[WARNING] EMPIAR-{num}: no se ha encontrado un fichero de gain "
            f"en {movies_dir_url or moviespath!r}. Revisa la entrada manualmente "
            f"y añade 'gain=' con la ruta correcta si aplica.",
            file=sys.stderr,
        )

    sr = (
        (imageset or {}).get("pixel_width")
        or (imageset or {}).get("pixel_height")
        or DEFAULTS["sr"]
    )

    sa, dose = DEFAULTS["sa"], DEFAULTS["dose"]

    for ref in entry.get("cross_references", []):
        if str(ref).upper().startswith("EMD-"):
            emd_sa, total_dose, emd_frames = get_emdb_microscopy(str(ref).upper())

            if emd_sa is not None:
                sa = emd_sa

            n_frames = (imageset or {}).get("frames_per_image") or emd_frames
            if total_dose is not None and n_frames:
                dose = round(total_dose / n_frames, 4)
            break

    return {
        "moviespath": moviespath,
        "filepatern": filepatern,
        "sa": sa,
        "ac": DEFAULTS["ac"],
        "sr": sr,
        "dose": dose,
        "gain": gain,
        "gainRot": gain_rot or 0,
        "gainFlip": gain_flip or 0,
    }


def format_params(params):
    return (
        f"moviespath='{params['moviespath']}' "
        f"filepatern='{params['filepatern']}' "
        f"sa='{params['sa']}' "
        f"ac='{params['ac']}' "
        f"sr='{params['sr']}' "
        f"dose='{params['dose']}' "
        f"gain='{params['gain']}' "
        f"gainRot='{params['gainRot']}' "
        f"gainFlip='{params['gainFlip']}'"
    )


def exec_scipion(params, template_path, display=":1", scipion_user_data="/home/scipionuser/ScipionUserData",
                 instance_name="scipion-spa"):
    
    cmd = ["apptainer", "exec", "--containall",
           "--env", f"DISPLAY={display}",
           "--env", f"SCIPION_USER_DATA={scipion_user_data}",
           "--bind", "/run",
           "--bind", "/tmp/.X11-unix",
           "--bind", "/etc/resolv.conf",
           "--bind", f"{scipion_user_data}",
           f"instance://{instance_name}",
           "/scipion/scipion3",
           "template",
           f"{template_path}"]

    for k, v in params.items():
        cmd.append(f"{k}={v}")

    print(f"Ejecutando: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def main():
    
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("empiar_id", help="ID de la entrada EMPIAR, p.ej. 10352 o EMPIAR-10352")
    parser.add_argument("--json", action="store_true", help="imprime JSON en vez de los parámetros del template")
    parser.add_argument("--template", action="store", help="ejecuta Scipion con los parámetros obtenidos, usando la plantilla indicada (ruta relativa a ScipionUserData)")
    parser.add_argument("--instance", action="store", help="Nombre de la instancia de Scipion", default="scipion-spa")
    parser.add_argument("--display", action="store", default=":1", help="valor de DISPLAY para ejecutar Scipion")
    parser.add_argument("--scipion-user-data", action="store", default=os.getcwd(), help=f"ruta a ScipionUserData")
    args = parser.parse_args()

    try:
        params = harvest(args.empiar_id, args.scipion_user_data)
    except requests.RequestException as e:
        print(f"Error consultando EMPIAR/EMDB: {e}", file=sys.stderr)
        sys.exit(1)

    if args.template:
        exec_scipion(params, args.template, instance_name=args.instance,
                     display=args.display, scipion_user_data=args.scipion_user_data)
    elif args.json:
        print(json.dumps(params, indent=4))
    else:
        print(format_params(params))


if __name__ == "__main__":
    main()
