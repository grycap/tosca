#!/usr/bin/env python3
"""
Obtiene de EMPIAR/EMDB los parámetros necesarios para lanzar el workflow
de Scipion "2D Streaming" (Xmipp):
https://workflowhub.eu/workflows/2169

    scipion3 template workflow_2D_xmipp.json.template \
        moviespath='...' sa='...' ac='...' sr='...' dose='...' gain='...'

Fuentes de datos:
  - EMPIAR REST API  (https://www.ebi.ac.uk/empiar/api/entry/<id>/)
      -> imageset de movies: directory, pixel size (sr)
  - EMPIAR FTP/HTTPS listing (https://ftp.ebi.ac.uk/empiar/world_availability/<id>/)
      -> localización real de los ficheros (la estructura de "directory" que
         devuelve la API no siempre coincide 1:1 con el árbol FTP, así que se
         verifica) y búsqueda del fichero de gain junto a las movies.
  - EMDB REST API (https://www.ebi.ac.uk/emdb/api/entry/<EMD-id>), a través de
    la cross-reference EMD-XXXX de la entrada EMPIAR
      -> spherical aberration (nominal_cs) y dosis por frame.

EMPIAR/EMDB no publican "amplitude contrast" (ac); se usa siempre el valor
por defecto habitual en crio-EM (0.1). Cuando cualquier otro dato no se
puede obtener, se rellena con el valor por defecto correspondiente.

Uso:
    python3 Harvesting_EMPIAR.py 10352
    python3 Harvesting_EMPIAR.py EMPIAR-10352 --json
"""

import argparse
import json
import re
import sys

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

GAIN_KEYWORDS = ["gain", "dark", "norm"]

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


def dir_exists(url):
    try:
        r = requests.head(url, timeout=TIMEOUT, allow_redirects=True)
        return r.status_code == 200
    except requests.RequestException:
        return False


def list_dir(url):
    """Devuelve [(nombre, es_directorio), ...] de un listado Apache/nginx."""
    try:
        r = requests.get(url, timeout=TIMEOUT)
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
    Busca en el árbol FTP de la entrada un directorio cuyo nombre coincida
    con leaf_name (nombre de carpeta reportado por la API de EMPIAR).
    Necesario porque la API no siempre refleja la ruta FTP real.
    """
    queue = [(root_url.rstrip("/") + "/", 0)]
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
            if name.rstrip("/").lower() == leaf_name.lower():
                return url + name
            queue.append((url + name, depth + 1))

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

        url = url + dirs[0]

    return url


def find_movies_directory(empiar_num, directory_hint):
    """
    Localiza la URL FTP real de la carpeta de movies.
    Prueba primero las dos convenciones observadas en EMPIAR:
      - <root>/<directory>/
      - <root>/data/<directory>/   (algunos depositantes anidan "data/data/…")
    y si ninguna existe, recorre el árbol buscando por nombre de carpeta.
    El resultado se refina por si las movies están un nivel más adentro.
    """
    root = EMPIAR_FTP_ROOT.format(id=empiar_num)

    if not directory_hint:
        return None

    directory_hint = directory_hint.strip("/")

    candidates = [
        f"{root}/{directory_hint}/",
        f"{root}/data/{directory_hint}/",
    ]

    for candidate in candidates:
        if dir_exists(candidate):
            return refine_to_files_dir(candidate)

    leaf = directory_hint.split("/")[-1]
    found = bfs_find_movies_dir(root, leaf)
    return refine_to_files_dir(found) if found else None


def parent_url(url):
    trimmed = url.rstrip("/")
    idx = trimmed.rfind("/")
    return trimmed[: idx + 1]


def is_gain_file(name):
    return any(k in name.lower() for k in GAIN_KEYWORDS) or name.lower().endswith(".gain")


def find_gain_file(empiar_num, movies_dir_url, max_levels_up=2):
    """
    Busca el fichero de gain en la carpeta de movies y, si no está ahí,
    en los directorios padre (hasta max_levels_up niveles, sin salir de la
    carpeta de la propia entrada EMPIAR) — es habitual que el gain se
    deposite junto a "data/" cubriendo varias subcarpetas de movies.
    """
    if not movies_dir_url:
        return None, None

    root = EMPIAR_FTP_ROOT.format(id=empiar_num).rstrip("/") + "/"
    url = movies_dir_url
    visited = set()

    for _ in range(max_levels_up + 1):
        if not url or url in visited:
            break
        visited.add(url)

        for name, is_dir in list_dir(url):
            if not is_dir and is_gain_file(name):
                return url, name

        if url == root:
            break
        url = parent_url(url)

    return None, None


def to_local_moviespath(empiar_num, movies_dir_url):
    root = EMPIAR_FTP_ROOT.format(id=empiar_num)
    suffix = movies_dir_url[len(root):]
    return f"./EMPIAR-{empiar_num}{suffix}"


def parse_float(value_of):
    try:
        return float(value_of)
    except (TypeError, ValueError):
        return None


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


def harvest(empiar_id):
    num = clean_id(empiar_id)
    entry = get_empiar_entry(num)

    imageset = pick_movie_imageset(entry.get("imagesets", []))
    directory_hint = (imageset or {}).get("directory")

    movies_dir_url = find_movies_directory(num, directory_hint)

    if movies_dir_url:
        moviespath = to_local_moviespath(num, movies_dir_url)
        gain_dir_url, gain_file = find_gain_file(num, movies_dir_url)
        gain = (
            f"{to_local_moviespath(num, gain_dir_url)}{gain_file}"
            if gain_file
            else ""
        )
    else:
        moviespath = f"./EMPIAR-{num}/data/{directory_hint}/" if directory_hint else ""
        gain = ""

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
        "sa": sa,
        "ac": DEFAULTS["ac"],
        "sr": sr,
        "dose": dose,
        "gain": gain,
    }


def format_params(params):
    return (
        f"moviespath='{params['moviespath']}' "
        f"sa='{params['sa']}' "
        f"ac='{params['ac']}' "
        f"sr='{params['sr']}' "
        f"dose='{params['dose']}' "
        f"gain='{params['gain']}'"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("empiar_id", help="ID de la entrada EMPIAR, p.ej. 10352 o EMPIAR-10352")
    parser.add_argument("--json", action="store_true", help="imprime JSON en vez de los parámetros del template")
    args = parser.parse_args()

    try:
        params = harvest(args.empiar_id)
    except requests.RequestException as e:
        print(f"Error consultando EMPIAR/EMDB: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(params, indent=4))
    else:
        print(format_params(params))


if __name__ == "__main__":
    main()

