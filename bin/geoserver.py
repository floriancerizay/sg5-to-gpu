"""Appels au GeoServer local (API REST et WMS), paramétrés par config.GEOSERVER_*."""

import json
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode

from config import GEOSERVER_PASSWORD, GEOSERVER_URL, GEOSERVER_USER

BASE_URL = GEOSERVER_URL.rstrip("/")
_AUTH = "Basic " + b64encode(f"{GEOSERVER_USER}:{GEOSERVER_PASSWORD}".encode()).decode()


def rest(path, method="GET", data=None, content_type=None):
    """Requête sur /rest/<path> → (code HTTP, corps) ; code 0 si GeoServer est injoignable."""
    req = urllib.request.Request(f"{BASE_URL}/rest/{path}", data=data, method=method)
    req.add_header("Authorization", _AUTH)
    if content_type:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except urllib.error.URLError as e:
        return 0, str(e).encode()


def ok(code):
    return code in (200, 201)


def recreate_workspace(name):
    """Supprime le workspace (et son contenu) puis le recrée → (code HTTP, corps) de la création."""
    delete_workspace(name)
    return rest("workspaces", "POST", json.dumps({"workspace": {"name": name}}).encode(), "application/json")


def delete_workspace(name):
    rest(f"workspaces/{name}?recurse=true", "DELETE")


def add_style(workspace, name, sld):
    """Publie un SLD (texte ou octets) dans le workspace → (code HTTP, corps)."""
    data = sld.encode("utf-8") if isinstance(sld, str) else sld
    return rest(f"workspaces/{workspace}/styles?name={name}", "POST", data, "application/vnd.ogc.sld+xml")


def add_geopackage(workspace, store, path):
    """Publie toutes les couches d'un GeoPackage dans un entrepôt du workspace → (code HTTP, corps)."""
    return rest(
        f"workspaces/{workspace}/datastores/{store}/file.gpkg?configure=all",
        "PUT",
        path.read_bytes(),
        "application/geopackage",
    )


def getmap(workspace, layer, style, bbox, size):
    """Image PNG d'une couche (EPSG:2154, fond blanc) → (octets, None) ou (None, message d'erreur)."""
    params = {
        "service": "WMS",
        "version": "1.1.1",
        "request": "GetMap",
        "layers": f"{workspace}:{layer}",
        "styles": f"{workspace}:{style}",
        "srs": "EPSG:2154",
        "bbox": ",".join(map(str, bbox)),
        "width": size[0],
        "height": size[1],
        "format": "image/png",
        "bgcolor": "0xFFFFFF",
    }
    url = f"{BASE_URL}/{workspace}/wms?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=300) as r:
        data = r.read()
        if not r.headers.get("Content-Type", "").startswith("image/"):
            return None, data[:400]
    return data, None
