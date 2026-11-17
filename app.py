"""Single-file app: Country Explorer.

Search any country and see its flag, capital, population, languages,
currencies, timezones, neighbours and more.

Run:  RESTCOUNTRIES_API_KEY=your_key python app.py     -> http://localhost:8000
Try:  http://localhost:8000/?name=Nepal
      http://localhost:8000/?random=1
JSON: http://localhost:8000/api/country?name=Nepal
Health check for K8s probes: /healthz

Data: REST Countries v5 (https://restcountries.com). It needs an API key, sent as
"Authorization: Bearer <key>". Get a free key at https://restcountries.com/sign-up.
If RESTCOUNTRIES_API_KEY is not set, the public demo key "rc_live_demo" is used
(sample data only, good enough to try the app).
"""
import os
import random
import socket
import time

import requests
from flask import Flask, jsonify, render_template_string, request
from markupsafe import escape

app = Flask(__name__)

API = "https://api.restcountries.com/countries/v5"
API_KEY = os.getenv("RESTCOUNTRIES_API_KEY", "rc_live_demo")
TIMEOUT = 8
CACHE_TTL = 24 * 3600  # country data barely changes; also saves your monthly quota
APP_VERSION = os.getenv("APP_VERSION", "1.0")
HOSTNAME = socket.gethostname()  # the pod name when running in Kubernetes

_cache = {}

FALLBACK_NAMES = [
    "Nepal", "India", "Japan", "Brazil", "Canada", "Egypt", "France", "Germany", "Kenya", "Mexico",
    "Norway", "Peru", "Thailand", "Australia", "Argentina", "Italy", "Spain", "Vietnam", "Iceland", "Morocco",
]


class UpstreamError(Exception):
    """The country API answered, but not with usable data."""


def cached(key, loader):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    value = loader()
    _cache[key] = (time.time(), value)  # exceptions skip this line, so failures are never cached
    return value


def api_get(path, params=None):
    """GET from the v5 API and return the list of country objects (may be empty)."""
    r = requests.get(
        f"{API}{path}",
        params=params,
        headers={"Authorization": f"Bearer {API_KEY}", "Accept": "application/json"},
        timeout=TIMEOUT,
    )
    if r.status_code == 404:
        return [], {}
    if not r.ok:
        raise UpstreamError(f"HTTP {r.status_code}: {r.text[:160]}")
    try:
        body = r.json()
    except ValueError:
        raise UpstreamError(f"not JSON (HTTP {r.status_code}): {r.text[:160]}")
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict) or not isinstance(data.get("objects"), list):
        raise UpstreamError(f"unexpected reply (HTTP {r.status_code}): {r.text[:160]}")
    return data["objects"], data.get("meta") or {}


def common_name(c):
    return (c.get("names") or {}).get("common", "")


def alpha3(c):
    return (c.get("codes") or {}).get("alpha_3", "")


def get_index():
    """All countries as {alpha3: {name}} for autocomplete and neighbour names.

    Never raises: on any trouble it logs and returns {}, so the rest of the app keeps
    working (just without autocomplete / neighbour names).
    """
    def load():
        index, offset = {}, 0
        for _ in range(6):  # ~250 countries at 100 per page = 3 pages; hard stop at 6
            objs, meta = api_get("", {"limit": 100, "offset": offset,
                                      "response_fields": "names.common,codes.alpha_3"})
            for c in objs:
                if alpha3(c) and common_name(c):
                    index[alpha3(c)] = {"name": common_name(c)}
            if not meta.get("more") or not objs:
                break
            offset += 100
        if not index:
            raise UpstreamError("country list came back empty")
        return index
    try:
        return cached("index", load)
    except (requests.RequestException, UpstreamError, ValueError, KeyError, TypeError) as e:
        app.logger.warning("country index unavailable: %s", e)
        return {}


def fetch_country(name=None, code=None):
    """Return one raw country dict, or None if not found."""
    def load():
        if code:
            objs, _ = api_get("/code", {"q": code})
            exact = [c for c in objs if alpha3(c).upper() == code.upper()
                     or (c.get("codes") or {}).get("alpha_2", "").upper() == code.upper()]
            objs = exact or objs
        else:
            try:
                objs, _ = api_get("/name", {"q": name, "limit": 25})
            except UpstreamError:
                objs = []
            if not objs:  # plain ?q= search on the base endpoint
                objs, _ = api_get("", {"q": name, "limit": 25})
        objs = [c for c in objs if isinstance(c, dict) and common_name(c)]
        if not objs:
            return None
        if name:
            exact = [c for c in objs if common_name(c).lower() == name.lower()]
            if exact:
                return exact[0]
        return max(objs, key=lambda c: c.get("population") or 0)
    return cached(f"c:{(code or name).lower()}", load)


def as_list(v):
    """The docs show some fields as lists and some as dicts; accept both."""
    if isinstance(v, dict):
        return list(v.values())
    return v if isinstance(v, list) else []


def shape(c, index):
    """Flatten the raw v5 record into what the page / JSON API needs."""
    names, flag = c.get("names") or {}, c.get("flag") or {}
    pop = c.get("population") or 0
    area = (c.get("area") or {}).get("kilometers") or 0
    calling = (as_list(c.get("calling_codes")) or [""])[0]
    if calling and not str(calling).startswith("+"):
        calling = f"+{calling}"

    currencies = []
    for cur in as_list(c.get("currencies")):
        if isinstance(cur, dict):
            label = cur.get("name") or cur.get("code") or ""
            symbol = cur.get("symbol") or cur.get("code") or ""
            currencies.append(f"{label} ({symbol})" if symbol else label)

    capitals = [x.get("name") if isinstance(x, dict) else str(x) for x in as_list(c.get("capitals"))]
    languages = sorted({l.get("name") if isinstance(l, dict) else str(l) for l in as_list(c.get("languages"))} - {None, ""})
    memberships = c.get("memberships") or {}

    return {
        "name": names.get("common", ""),
        "official": names.get("official", ""),
        "cca2": (c.get("codes") or {}).get("alpha_2", ""),
        "cca3": alpha3(c),
        "flag": flag.get("url_svg") or flag.get("url_png", ""),
        "flag_alt": flag.get("description") or "",
        "emoji": flag.get("emoji", ""),
        "capital": ", ".join(x for x in capitals if x) or "None",
        "region": c.get("region", ""),
        "subregion": c.get("subregion", ""),
        "population": pop,
        "area": area,
        "density": round(pop / area, 1) if area else None,
        "languages": languages,
        "currencies": currencies,
        "timezones": as_list(c.get("timezones")),
        "calling": calling,
        "tld": ", ".join(as_list(c.get("tlds"))),
        "landlocked": bool(c.get("landlocked", False)),
        "un_member": bool(memberships.get("un") or (c.get("classification") or {}).get("un_member")),
        "maps": (c.get("links") or {}).get("google_maps", ""),
        "borders": [{"code": b, "name": index.get(b, {}).get("name", b)} for b in as_list(c.get("borders"))],
    }


PAGE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#f6f1e7">
  <title>{{ c.name + ' · ' if c else '' }}Country Explorer</title>
  <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='.9em' font-size='90'%3E%F0%9F%8C%8D%3C/text%3E%3C/svg%3E">
  <style>
    :root {
      --bg: #f6f1e7; --paper: #fffdf8; --ink: #1f1b16; --muted: #6f665a;
      --line: #e4dccb; --accent: #c2410c; --accent-soft: #fde6d6; --shadow: 0 1px 0 #fff inset, 0 18px 40px -18px rgba(60,40,10,.35);
    }
    @media (prefers-color-scheme: dark) {
      :root { --bg: #14110d; --paper: #1d1913; --ink: #f4ede0; --muted: #a39a8b;
              --line: #322b20; --accent: #fb923c; --accent-soft: #3a2415; --shadow: 0 18px 40px -18px rgba(0,0,0,.7); }
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; min-height: 100vh; color: var(--ink); background: var(--bg);
      background-image: radial-gradient(circle at 12% -10%, var(--accent-soft), transparent 45%),
                        radial-gradient(circle at 100% 110%, var(--accent-soft), transparent 40%);
      font-family: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
      line-height: 1.5; padding: 32px 16px 48px;
    }
    .wrap { max-width: 880px; margin: 0 auto; }
    h1 { font-size: clamp(1.9rem, 5vw, 2.6rem); margin: 0; letter-spacing: -.02em; }
    h1 span { color: var(--accent); font-style: italic; }
    .sub { color: var(--muted); margin: 4px 0 22px; font-family: system-ui, sans-serif; font-size: .95rem; }

    form.search { display: flex; gap: 8px; flex-wrap: wrap; font-family: system-ui, sans-serif; }
    .search input {
      flex: 1 1 240px; min-width: 0; padding: 14px 18px; font: inherit; font-size: 1rem; color: var(--ink);
      background: var(--paper); border: 1.5px solid var(--line); border-radius: 14px; outline: none; transition: .15s;
    }
    .search input:focus { border-color: var(--accent); box-shadow: 0 0 0 4px var(--accent-soft); }
    .btn {
      padding: 14px 20px; font: inherit; font-weight: 600; font-size: .95rem; border-radius: 14px; cursor: pointer;
      border: 1.5px solid var(--line); background: var(--paper); color: var(--ink); text-decoration: none; transition: .15s;
      display: inline-flex; align-items: center; justify-content: center;
    }
    .btn:hover { transform: translateY(-1px); border-color: var(--accent); }
    .btn.primary { background: var(--accent); border-color: var(--accent); color: #fff; }

    .card { margin-top: 24px; background: var(--paper); border: 1px solid var(--line); border-radius: 24px;
            box-shadow: var(--shadow); overflow: hidden; animation: rise .5s cubic-bezier(.2,.8,.2,1) both; }
    @keyframes rise { from { opacity: 0; transform: translateY(14px); } }
    .hero { display: grid; grid-template-columns: 1.1fr 1fr; }
    .hero .flag { background: var(--accent-soft); display: flex; align-items: center; justify-content: center; padding: 28px; }
    .hero .flag img { width: 100%; max-height: 220px; object-fit: contain; border-radius: 6px;
                      box-shadow: 0 10px 30px -8px rgba(0,0,0,.35); }
    .hero .title { padding: 28px; display: flex; flex-direction: column; justify-content: center; }
    .title h2 { margin: 0; font-size: clamp(1.8rem, 5vw, 2.4rem); letter-spacing: -.02em; line-height: 1.1; }
    .title .official { color: var(--muted); font-style: italic; margin-top: 6px; }
    .tags { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 14px; font-family: system-ui, sans-serif; }
    .tag { font-size: .75rem; font-weight: 600; padding: 4px 10px; border-radius: 999px;
           background: var(--accent-soft); color: var(--accent); letter-spacing: .02em; }

    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); border-top: 1px solid var(--line); }
    .cell { padding: 18px 24px; border-right: 1px solid var(--line); border-bottom: 1px solid var(--line); }
    .cell .k { font-family: system-ui, sans-serif; font-size: .7rem; text-transform: uppercase; letter-spacing: .1em; color: var(--muted); }
    .cell .v { font-size: 1.15rem; margin-top: 4px; word-break: break-word; }
    .cell .v.big { font-size: 1.5rem; font-weight: 700; font-variant-numeric: tabular-nums; }

    .section { padding: 20px 24px 24px; }
    .section h3 { margin: 0 0 10px; font-family: system-ui, sans-serif; font-size: .7rem; text-transform: uppercase;
                  letter-spacing: .1em; color: var(--muted); }
    .chips { display: flex; flex-wrap: wrap; gap: 8px; font-family: system-ui, sans-serif; }
    .chip { padding: 7px 14px; border-radius: 999px; border: 1.5px solid var(--line); background: var(--bg);
            color: var(--ink); text-decoration: none; font-size: .88rem; transition: .15s; }
    a.chip:hover { border-color: var(--accent); color: var(--accent); transform: translateY(-1px); }
    .none { color: var(--muted); font-style: italic; }
    .actions { padding: 0 24px 24px; display: flex; gap: 8px; flex-wrap: wrap; }

    .demo { margin: 0 0 14px; padding: 12px 16px; border-radius: 14px; background: var(--accent-soft); color: var(--ink);
            border: 1px solid var(--accent); font-family: system-ui, sans-serif; font-size: .88rem; }
    .demo code { background: var(--paper); padding: 1px 6px; border-radius: 6px; }
    .error { margin-top: 24px; padding: 24px; border-radius: 20px; background: var(--paper);
             border: 1.5px dashed var(--line); text-align: center; color: var(--muted); }
    .error b { color: var(--ink); }
    footer { margin-top: 26px; text-align: center; color: var(--muted); font-family: system-ui, sans-serif; font-size: .75rem; }
    footer code { background: var(--accent-soft); color: var(--accent); padding: 2px 8px; border-radius: 6px; }

    @media (max-width: 640px) {
      .hero { grid-template-columns: 1fr; }
      .cell { border-right: 0; }
    }
    @media (prefers-reduced-motion: reduce) { * { animation: none !important; transition: none !important; } }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Country <span>Explorer</span></h1>
    <p class="sub">Search any country in the world.</p>

    {% if demo %}<div class="demo">Demo mode: no API key set, so only sample data (Canada) is available.
      Set <code>RESTCOUNTRIES_API_KEY</code> to search every country.</div>{% endif %}

    <form class="search" method="get" role="search">
      <input name="name" list="countries" placeholder="Try Nepal, Japan, Brazil…" value="{{ query }}"
             aria-label="Country name" autocomplete="off" autofocus>
      <datalist id="countries">{% for n in names %}<option value="{{ n }}">{% endfor %}</datalist>
      <button class="btn primary" type="submit">Search</button>
      <a class="btn" href="/?random=1">🎲 Random</a>
    </form>

    {% if c %}
    <article class="card">
      <div class="hero">
        <div class="flag">{% if c.flag %}<img src="{{ c.flag }}" alt="{{ c.flag_alt or ('Flag of ' + c.name) }}">{% endif %}</div>
        <div class="title">
          <h2>{{ c.emoji }} {{ c.name }}</h2>
          <div class="official">{{ c.official }}</div>
          <div class="tags">
            {% if c.region %}<span class="tag">{{ c.region }}</span>{% endif %}
            {% if c.subregion %}<span class="tag">{{ c.subregion }}</span>{% endif %}
            {% if c.un_member %}<span class="tag">UN member</span>{% endif %}
            {% if c.landlocked %}<span class="tag">Landlocked</span>{% endif %}
          </div>
        </div>
      </div>

      <div class="grid">
        <div class="cell"><div class="k">Capital</div><div class="v">{{ c.capital }}</div></div>
        <div class="cell"><div class="k">Population</div><div class="v big">{{ '{:,}'.format(c.population) }}</div></div>
        <div class="cell"><div class="k">Area</div><div class="v big">{{ '{:,.0f}'.format(c.area) }} <small>km²</small></div></div>
        <div class="cell"><div class="k">Density</div><div class="v">{% if c.density is not none %}{{ '{:,}'.format(c.density) }} / km²{% else %}n/a{% endif %}</div></div>
        <div class="cell"><div class="k">Calling code</div><div class="v">{{ c.calling or 'n/a' }}</div></div>
        <div class="cell"><div class="k">Domain</div><div class="v">{{ c.tld or 'n/a' }}</div></div>
      </div>

      <div class="section"><h3>Languages</h3>
        <div class="chips">{% for l in c.languages %}<span class="chip">{{ l }}</span>{% else %}<span class="none">None listed</span>{% endfor %}</div>
      </div>
      <div class="section"><h3>Currencies</h3>
        <div class="chips">{% for x in c.currencies %}<span class="chip">{{ x }}</span>{% else %}<span class="none">None listed</span>{% endfor %}</div>
      </div>
      <div class="section"><h3>Timezones</h3>
        <div class="chips">{% for t in c.timezones %}<span class="chip">{{ t }}</span>{% endfor %}</div>
      </div>
      <div class="section"><h3>Neighbours</h3>
        <div class="chips">{% for b in c.borders %}<a class="chip" href="/?code={{ b.code }}">{{ b.name }}</a>{% else %}<span class="none">No land borders (island or similar)</span>{% endfor %}</div>
      </div>
      {% if c.maps %}<div class="actions"><a class="btn" href="{{ c.maps }}" target="_blank" rel="noopener">📍 Open in Google Maps</a></div>{% endif %}
    </article>
    {% elif error %}
    <div class="error">{{ error|safe }}</div>
    {% endif %}

    <footer>
      Data by <a href="https://restcountries.com" target="_blank" rel="noopener">REST Countries</a>
      &middot; v{{ version }} &middot; served by <code>{{ host }}</code>
    </footer>
  </div>
</body>
</html>
"""


def lookup(args):
    """Resolve the request into (shaped_country | None, error_html | None, query_text)."""
    index = get_index()
    code = (args.get("code") or "").strip().upper()[:3]
    name = (args.get("name") or "").strip()[:80]

    if args.get("random"):
        if index:
            code = random.choice(list(index))
        else:
            name = random.choice(FALLBACK_NAMES)
    # user input is echoed into the error text (rendered with |safe), so escape it
    if code:
        raw = fetch_country(code=code)
        if raw:
            shaped = shape(raw, index)
            return shaped, None, shaped["name"]
        return None, f"No country with code <b>{escape(code)}</b>.", ""
    if name:
        raw = fetch_country(name=name)
        if raw:
            shaped = shape(raw, index)
            return shaped, None, shaped["name"]
        return None, f"Couldn't find a country called <b>{escape(name)}</b>. Check the spelling or pick one from the suggestions.", name
    return None, None, ""


def safe_lookup(args):
    try:
        return lookup(args)
    except UpstreamError as e:
        app.logger.warning("upstream problem: %s", e)
        return None, f"The country service replied with something unexpected:<br><code>{escape(str(e))}</code>", (args.get("name") or "")
    except Exception as e:  # never show a 500 page for upstream trouble
        app.logger.exception("lookup failed: %s", e)
        return None, "Couldn't get data from the country service. Please try again in a moment.", (args.get("name") or "")


@app.route("/")
def home():
    c, error, query = safe_lookup(request.args)
    names = sorted(v["name"] for v in get_index().values())
    if c is None and error is None:
        # landing page: show a friendly default
        c, error, query = safe_lookup({"name": os.getenv("DEFAULT_COUNTRY", "Nepal")})
    return render_template_string(PAGE, c=c, error=error, query=query, names=names,
                                  version=APP_VERSION, host=HOSTNAME, demo=(API_KEY == "rc_live_demo"))


@app.route("/api/country")
def api_country():
    c, error, _ = safe_lookup(request.args)
    if c is None:
        return jsonify(error=(error or "provide ?name= or ?code=")), 404
    return jsonify(c)


@app.route("/api/countries")
def api_countries():
    names = sorted(v["name"] for v in get_index().values())
    if not names:
        return jsonify(error="country list unavailable"), 502
    return jsonify(names)


@app.route("/healthz")
def healthz():
    return jsonify(status="ok", version=APP_VERSION, pod=HOSTNAME)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))