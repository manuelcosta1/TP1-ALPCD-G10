from urllib.parse import urljoin
from typing import Dict, Any, Optional, List
import requests
import typer
import json
import re
import csv
from datetime import date
import unicodedata
from html import unescape


API_CONFIG: Dict[str, Any] = {
    "api": {
        "base_url": "https://api.itjobs.pt/job/",
        "key": "a6ff6adf6fd1acdd78e4e80a735fe20d",
        "endpoints": {
            "get": "get.json",
            "list": "list.json",
            "search": "search.json",
            "status": "status.json",
        },
    }
}


def _get_api_key() -> str:
    return API_CONFIG["api"].get("key", "")


def _build_url(name: str) -> str:
    api = API_CONFIG["api"]
    endpoints = api.get("endpoints", {})
    if name not in endpoints:
        raise KeyError(f"Unknown endpoint: {name}")

    path = endpoints[name]
    return urljoin(api["base_url"], path)


def _get_headers() -> Dict[str, str]:
    headers: Dict[str, str] = {}
    headers.setdefault(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:115.0) Gecko/20100101 Firefox/115.0",
    )
    return headers


def _get_params(extra_params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    api_key = _get_api_key()
    if api_key:
        params.setdefault("api_key", api_key)
    if extra_params:
        params.update(extra_params)
    return params

def list_jobs(limit: int):
    params = _get_params({"limit": limit})
    headers = _get_headers()

    url = _build_url("list")

    resp = requests.get(url, params=params, headers=headers)

    resp.raise_for_status()

    return resp

TEAMLYZER_UA = {"User-Agent": "Mozilla/5.0 (compatible; TeamlyzerScraper/1.0)"}
TEAMLYZER_BASE = "https://pt.teamlyzer.com"


def _norm(s: str) -> str:
    """Normalize string for fuzzy matching (lower, remove accents, trim spaces)."""
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"\s+", " ", s)
    return s


def _html_to_text(html: str) -> str:
    """Very simple HTML -> text."""
    html = unescape(html or "")
    # remove scripts/styles
    html = re.sub(r"(?is)<script.*?>.*?</script>", " ", html)
    html = re.sub(r"(?is)<style.*?>.*?</style>", " ", html)
    # replace <br> and </p> with newlines
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</p\s*>", "\n", html)
    # strip tags
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    # normalize whitespace
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def _extract_company_name(job_json: Dict[str, Any]) -> str:
    """Try hard to extract company name from itjobs job payload."""
    if not isinstance(job_json, dict):
        return ""
    # common patterns in APIs
    for key in ("company", "empresa"):
        val = job_json.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, dict):
            for k2 in ("name", "nome", "title"):
                v2 = val.get(k2)
                if isinstance(v2, str) and v2.strip():
                    return v2.strip()

    # fallback: some APIs store as "company_name"
    for key in ("company_name", "companyName", "empresa_nome"):
        v = job_json.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()

    return ""


def _pick_job_field(job: Dict[str, Any], *keys: str) -> str:
    """Try multiple keys and return first non-empty string."""
    for k in keys:
        v = job.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):
            # some APIs nest location/company fields
            for kk in ("name", "title", "nome"):
                vv = v.get(kk)
                if isinstance(vv, str) and vv.strip():
                    return vv.strip()
    return ""

def _find_teamlyzer_company_slug(company_name: str, max_pages_fallback: int = 3) -> Optional[str]:
    """
    Find company slug on Teamlyzer.
    1) Try /companies/ranking (Top 50)
    2) Fallback: try /companies/ pages (first N pages only)
    Returns slug like "blip" or "pwc".
    """
    target = _norm(company_name)
    if not target:
        return None

    # helper: extract (slug, display_name) pairs from HTML
    def extract_pairs(html: str) -> List[tuple]:
        pairs = []
        # Teamlyzer company profile links are typically /companies/<slug>
        for m in re.finditer(r'href="(/companies/([a-z0-9\-]+))"', html, flags=re.I):
            slug = m.group(2)
            # avoid obvious non-company pages
            if slug in {"ranking", "awards", "jobs", "remote-companies"}:
                continue
            pairs.append((slug, slug))
        # de-dup keep order
        seen = set()
        out = []
        for slug, name in pairs:
            if slug not in seen:
                seen.add(slug)
                out.append((slug, name))
        return out

    # 1) ranking
    try:
        r = requests.get(f"{TEAMLYZER_BASE}/companies/ranking", headers=TEAMLYZER_UA, timeout=20)
        r.raise_for_status()
        html = r.text
        # ranking page contains company names in visible text; easiest is to textify and check around slugs
        text = _html_to_text(html)
        # brute: if company name appears, also try to find a nearby /companies/<slug> in raw html
        if target in _norm(text):
            # try to guess slug by scanning links and checking if slug appears in company name
            for slug, _ in extract_pairs(html):
                if slug and slug in _norm(company_name).replace(" ", "-"):
                    return slug
        # safer: try to match by visiting likely slug links would be too heavy;
        # instead do a lightweight heuristic:
        # If company is "Blip.pt", often slug is "blip". Remove punctuation and try.
        guess = re.sub(r"[^a-z0-9\-]+", "", _norm(company_name).replace(" ", "-"))
        if guess:
            # check if that slug link exists in ranking html
            if re.search(rf'href="/companies/{re.escape(guess)}"', html, flags=re.I):
                return guess
    except Exception:
        pass

    # 2) fallback: scan first N pages of /companies/
    for page in range(1, max_pages_fallback + 1):
        try:
            url = f"{TEAMLYZER_BASE}/companies/"
            if page != 1:
                url = f"{TEAMLYZER_BASE}/companies/?page={page}"
            r = requests.get(url, headers=TEAMLYZER_UA, timeout=20)
            r.raise_for_status()
            html = r.text
            text = _html_to_text(html)

            # if company name is visible on the page, try to extract the nearest slug by a guess
            if target in _norm(text):
                guess = re.sub(r"[^a-z0-9\-]+", "", _norm(company_name).replace(" ", "-"))
                if guess and re.search(rf'href="/companies/{re.escape(guess)}"', html, flags=re.I):
                    return guess

                # otherwise try: find any slug whose normalized slug matches part of the company name
                for slug, _ in extract_pairs(html):
                    if slug and slug in _norm(company_name).replace(" ", "-"):
                        return slug

                # last resort: pick first slug that appears close to the company name in text
                # (weak but sometimes works)
                for slug, _ in extract_pairs(html):
                    if slug and re.search(rf"\b{re.escape(slug)}\b", _norm(company_name).replace(" ", "-")):
                        return slug
        except Exception:
            continue

    return None


def _scrape_teamlyzer_company(slug: str, top_benefits: int = 5) -> Dict[str, Any]:
    """
    Scrape rating, description, salary and benefits from Teamlyzer company pages.
    """
    out: Dict[str, Any] = {
        "teamlyzer_rating": None,
        "teamlyzer_description": None,
        "teamlyzer_benefits": [],
        "teamlyzer_salary": None,
    }

    if not slug:
        return out

    # company overview page
    try:
        r = requests.get(f"{TEAMLYZER_BASE}/companies/{slug}", headers=TEAMLYZER_UA, timeout=20)
        r.raise_for_status()
        text = _html_to_text(r.text)

        # rating: first match like 3.4/5
        m = re.search(r"(\d(?:\.\d)?)\s*/\s*5", text)
        if m:
            try:
                out["teamlyzer_rating"] = float(m.group(1))
            except Exception:
                out["teamlyzer_rating"] = m.group(1)

        # description: usually appears near the top; take a slice after the first lines with company meta
        # heuristic: grab the first “paragraph-like” sentence block > 30 chars before the skills list
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        # try to find the first long line after the company name line
        desc = None
        for ln in lines[:40]:
            if len(ln) >= 60 and not re.search(r"/5|Reviews|Visão geral|Emprego|Entrevista|Salário|Seguir", ln):
                desc = ln
                break
        if desc:
            out["teamlyzer_description"] = desc

        # salary: try to capture "está entre os X€ e Y€"
        ms = re.search(r"sal[aá]rio m[eé]dio.*?entre\s+os\s+([^\.]+?)\s+e\s+([^\.]+?)\.", text, flags=re.I)
        if ms:
            out["teamlyzer_salary"] = f"entre {ms.group(1).strip()} e {ms.group(2).strip()}"
    except Exception:
        pass

    # benefits page (benefits-and-values)
    try:
        r = requests.get(f"{TEAMLYZER_BASE}/companies/{slug}/benefits-and-values", headers=TEAMLYZER_UA, timeout=20)
        r.raise_for_status()
        text = _html_to_text(r.text)

        # extract bullet lines under "Benefícios e vantagens"
        # In text, bullets show as " * Benefit title"
        benefits = []
        in_benefits = False
        for ln in text.split("\n"):
            s = ln.strip()
            if re.search(r"Benef[ií]cios e vantagens", s, flags=re.I):
                in_benefits = True
                continue
            if in_benefits and re.search(r"Valores e cultura", s, flags=re.I):
                break
            if in_benefits and s.startswith("* "):
                title = s[2:].strip()
                # ignore very short/noisy lines
                if len(title) >= 3:
                    benefits.append(title)

        # keep unique, top N
        seen = set()
        uniq = []
        for b in benefits:
            nb = _norm(b)
            if nb and nb not in seen:
                seen.add(nb)
                uniq.append(b)
        out["teamlyzer_benefits"] = uniq[:top_benefits]
    except Exception:
        pass

    return out


def enrich_job_with_teamlyzer(job_json: Dict[str, Any], fallback_pages: int = 3) -> Dict[str, Any]:
    """Return a copy of job_json enriched with Teamlyzer fields."""
    if not isinstance(job_json, dict):
        return {"data": job_json}

    company_name = _extract_company_name(job_json)
    slug = _find_teamlyzer_company_slug(company_name, max_pages_fallback=fallback_pages) if company_name else None
    info = _scrape_teamlyzer_company(slug or "", top_benefits=5)

    enriched = dict(job_json)
    enriched.update(info)
    return enriched


app = typer.Typer()

list_app = typer.Typer()
app.add_typer(list_app, name="list")



def top_jobs(limit: int) -> list:
    resp = list_jobs(limit)
    data = resp.json()
    jobs = _extract_jobs_from_response(data)
    return jobs


@app.command("top")
def top(
    limit: int = typer.Argument(..., help="Number of jobs to fetch"),
    csv_path: Optional[str] = typer.Option(None, "--csv", help="Path to CSV file to export results"),
):
    """List the N most recent jobs and print JSON array to stdout.

    Usage: `python jobscli.py top 30` or `python jobscli.py top 30 --csv out.csv`
    """
    try:
        jobs = top_jobs(limit)
    except Exception as e:
        typer.echo(f"Error fetching jobs: {e}", err=True)
        raise typer.Exit(code=1)

    typer.echo(json.dumps(jobs, indent=2, ensure_ascii=False))

    if csv_path:
        try:
            _export_to_csv(jobs, csv_path)
            typer.echo(f"Exported {len(jobs)} jobs to {csv_path}")
        except Exception as e:
            typer.echo(f"Error exporting CSV: {e}", err=True)
            raise typer.Exit(code=1)


def search_jobs(q: str, limit: int, extra_params: Optional[Dict[str, Any]] = None):
    """Call the `search` endpoint with query `q` and return a `requests.Response`.
    """
    params = _get_params(extra_params)
    params["limit"] = limit
    params["q"] = q

    headers = _get_headers()

    url = _build_url("search")
    resp = requests.get(url, params=params, headers=headers)
    resp.raise_for_status()
    
    return resp


@app.command("search")
def search(
    q: str = typer.Argument(..., help="Search query (required)"),
    limit: int = typer.Option(10, "--limit", "-l", help="Number of results (default: 10)"),
    company: str = typer.Option(None, "--company", "-c", help="Filter by company name"),
    type_: str = typer.Option(None, "--type", "-t", help="Filter by job type"),
    contract: str = typer.Option(None, "--contract", help="Filter by contract type"),
    page: int = typer.Option(1, "--page", "-p", help="Page number (default: 1)"),
):
    """Search jobs by query and optional filters.

    CLI usage: `python jobscli.py search "QUERY" [--limit 20] [--company "X"] [--type "full-time"] [--contract "permanent"] [--page 2]`
    """
    try:
        # Build extra params from optional filters
        extra = {}
        if company:
            extra["company"] = company
        if type_:
            extra["type"] = type_
        if contract:
            extra["contract"] = contract
        if page != 1:
            extra["page"] = page

        resp = search_jobs(q, limit=limit, extra_params=extra)
        data = resp.json()
    except Exception as e:
        typer.echo(f"Error performing search: {e}", err=True)
        raise typer.Exit(code=1)

    typer.echo(json.dumps(data, indent=2, ensure_ascii=False))

@app.command("get")
def get(
    job_id: str = typer.Argument(..., help="Job ID (required)"),
    teamlyzer_pages: int = typer.Option(3, "--teamlyzer-pages", help="How many /companies pages to scan if not found in ranking (default: 3)"),
):
    """Get job details by ID and enrich with Teamlyzer company info.

    Example:
      python jobscli.py get 125378
    """
    try:
        resp = get_job(job_id)
        data = resp.json()

        # API sometimes returns wrapper dict; try to locate the job dict inside
        job_obj = data
        if isinstance(data, dict):
            for k in ("job", "data", "result"):
                if isinstance(data.get(k), dict):
                    job_obj = data[k]
                    break

        enriched = enrich_job_with_teamlyzer(job_obj, fallback_pages=teamlyzer_pages)
        typer.echo(json.dumps(enriched, indent=2, ensure_ascii=False))
    except Exception as e:
        typer.echo(f"Error fetching/enriching job: {e}", err=True)
        raise typer.Exit(code=1)

def _extract_zone_from_job(job: Dict[str, Any]) -> str:
    # itjobs costuma ter locations como lista de dicts no detalhe
    locs = job.get("locations")
    if isinstance(locs, list) and locs:
        first = locs[0]
        if isinstance(first, dict):
            name = first.get("name") or first.get("title") or first.get("nome")
            if isinstance(name, str) and name.strip():
                return name.strip()
        if isinstance(first, str) and first.strip():
            return first.strip()

    # fallback: campos comuns
    for k in ("location", "localizacao", "localidade", "city", "region", "zona"):
        v = job.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()

    return "Unknown"


def _extract_type_from_job(job: Dict[str, Any]) -> str:
    # no detalhe, tipo pode vir em vários formatos
    for k in ("type", "tipo", "employment_type", "contract_type", "contract"):
        v = job.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()

    # às vezes vem como lista (ex: types)
    types = job.get("types")
    if isinstance(types, list) and types:
        t0 = types[0]
        if isinstance(t0, dict):
            name = t0.get("name") or t0.get("title")
            if isinstance(name, str) and name.strip():
                return name.strip()
        if isinstance(t0, str) and t0.strip():
            return t0.strip()

    return "Unknown"


@app.command("statistics")
def statistics(
    group: str = typer.Argument(..., help="Grouping mode (use: zone)"),
    limit: int = typer.Option(200, "--limit", "-l", help="Max jobs to scan (default: 200)"),
    out: str = typer.Option("statistics.csv", "--out", "-o", help="CSV output path"),
):
    """
    Create CSV statistics counting vacancies by Zone and Job Type.
    Uses list.json to get IDs and get.json to extract reliable fields.
    """
    group = (group or "").strip().lower()
    if group != "zone":
        typer.echo("Neste trabalho, usa: statistics zone", err=True)
        raise typer.Exit(code=1)

    # 1) buscar lista (IDs)
    try:
        resp = list_jobs(limit)
        data = resp.json()
        jobs = _extract_jobs_from_response(data)
    except Exception as e:
        typer.echo(f"Error fetching jobs list: {e}", err=True)
        raise typer.Exit(code=1)

    counts: Dict[tuple, int] = {}

    # 2) para cada job, buscar detalhe (get.json) e extrair zona/tipo
    for j in jobs:
        job_id = j.get("id") or j.get("job_id")
        if not job_id:
            continue

        try:
            detail = get_job(str(job_id)).json()

            # por vezes vem embrulhado
            job_obj = detail
            if isinstance(detail, dict):
                for k in ("job", "data", "result"):
                    if isinstance(detail.get(k), dict):
                        job_obj = detail[k]
                        break

            zone = _extract_zone_from_job(job_obj)
            job_type = _extract_type_from_job(job_obj)

            key = (zone, job_type)
            counts[key] = counts.get(key, 0) + 1

        except Exception:
            # se falhar um job, ignoramos e seguimos
            continue

    # 3) exportar CSV
    try:
        with open(out, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["Zona", "Tipo de Trabalho", "Nº de vagas"])

            for (zone, job_type), n in sorted(
                counts.items(),
                key=lambda kv: (kv[0][0].lower(), -kv[1], kv[0][1].lower()),
            ):
                writer.writerow([zone, job_type, n])

        typer.echo("Ficheiro de exportação criado com sucesso.")
    except Exception as e:
        typer.echo(f"Error writing CSV: {e}", err=True)
        raise typer.Exit(code=1)



def get_job(job_id: str):
    """Call the `get` endpoint to fetch job details by ID."""
    params = _get_params({"id": job_id})
    headers = _get_headers()
    
    url = _build_url("get")
    resp = requests.get(url, params=params, headers=headers)
    resp.raise_for_status()
    
    return resp


def extract_work_regime(job_data: Dict[str, Any]) -> str:
    """Extract work regime (remote/hybrid/on-site/other) from job data."""
    text = json.dumps(job_data).lower()
    
    # Patterns for different work regimes
    if re.search(r"\bremoto\b|\bremote\b", text):
        return "remote"
    elif re.search(r"\bh[íi]brido\b|\bhybrid\b", text):
        return "hybrid"
    elif re.search(r"\bpresencial\b|\bon-?site\b|\bfísico\b", text):
        return "on-site"
    else:
        return "other"


def _extract_jobs_from_response(data: Any) -> List[Dict[str, Any]]:
    """Try to find and return the list of jobs from API response data."""
    # If the response is already a list, assume it's the jobs list
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        # common keys that may contain job lists
        for key in ("jobs", "results", "data", "items"):
            val = data.get(key)
            if isinstance(val, list):
                return val

        # fallback: return the first value that is a list
        for val in data.values():
            if isinstance(val, list):
                return val

    return []


def _normalize_job_for_csv(job: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the CSV fields from a job dict safely.

    Fields: titulo, empresa, descricao, data de publicacao, salario, localizacao
    """
    # Helpers to try multiple possible keys
    def pick(obj: Dict[str, Any], *keys):
        for k in keys:
            v = obj.get(k)
            if v:
                return v
        return ""

    title = pick(job, "title", "titulo", "job_title")
    company = pick(job, "company", "empresa", "company_name")
    description = pick(job, "description", "descricao", "job_description")
    # date fields may be named differently
    pub_date = pick(job, "date", "date_published", "published_at", "data")
    salary = pick(job, "salary", "salario")
    location = pick(job, "location", "localidade", "city")

    return {
        "titulo": str(title),
        "empresa": str(company),
        "descricao": str(description),
        "data_de_publicacao": str(pub_date),
        "salario": str(salary),
        "localizacao": str(location),
    }

from urllib.parse import quote_plus, unquote_plus


def _teamlyzer_jobs_search_url(query: str, page: int = 1) -> str:
    # URL base recomendada pelo enunciado / exemplo
    q = quote_plus(query.strip())
    url = f"{TEAMLYZER_BASE}/companies/jobs?search={q}&order=most_relevant"
    if page != 1:
        url += f"&page={page}"
    return url


def _extract_skill_tags_from_teamlyzer_jobs_html(html: str) -> List[str]:
    """
    Extract skill tags from Teamlyzer jobs HTML.
    Teamlyzer tag links usually look like:
      /companies/jobs?tags=python&order=most_relevant
    We count these tags as "skills".
    """
    tags = []
    # capture tags param in href
    for m in re.finditer(r'href="\/companies\/jobs\?[^"]*tags=([^"&]+)', html, flags=re.I):
        raw = m.group(1)
        tag = unquote_plus(raw).strip().lower()
        # sometimes tags can be comma-separated; split if needed
        for t in re.split(r"[,\|]", tag):
            t = t.strip().lower()
            if t:
                tags.append(t)
    return tags


def _export_to_csv(jobs: List[Dict[str, Any]], path: str) -> None:
    """Write a list of job dicts to CSV at `path` using normalized fields."""
    if not jobs:
        # create empty file with headers
        headers = ["titulo", "empresa", "descricao", "data_de_publicacao", "salario", "localizacao"]
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=headers)
            writer.writeheader()
        return

    rows = [_normalize_job_for_csv(j) for j in jobs]
    headers = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)



@app.command("type")
def work_type(job_id: str = typer.Argument(..., help="Job ID (required)")):
    """Extract work regime from a specific job.
    
    CLI usage: `python jobscli.py type 12345`
    """
    try:
        resp = get_job(job_id)
        data = resp.json()
        regime = extract_work_regime(data)
        typer.echo(regime)
    except Exception as e:
        typer.echo(f"Error fetching job: {e}", err=True)
        raise typer.Exit(code=1)


@app.command("list-company")
def list_company(
    location: str = typer.Argument(..., help="Job location (required)"),
    company: str = typer.Argument(..., help="Company name (required)"),
    limit: int = typer.Argument(50, help="Number of jobs to fetch (required)"),
    csv_path: Optional[str] = typer.Option(None, "--csv", help="Path to CSV file to export"),
):
    """List part-time jobs from a company in a specific location.

    CLI usage: `python jobscli.py list-company Porto EmpresaY 3 [--csv out.csv]`
    """
    try:
        extra = {
            "type": "part-time",
            "company": company,
            "location": location,
        }
        # Use company as query to increase chance of relevant results
        resp = search_jobs(company, limit=limit, extra_params=extra)
        data = resp.json()
        jobs = _extract_jobs_from_response(data)
    except Exception as e:
        typer.echo(f"Error fetching jobs: {e}", err=True)
        raise typer.Exit(code=1)

    typer.echo(json.dumps(jobs, indent=2, ensure_ascii=False))

    if csv_path:
        try:
            _export_to_csv(jobs, csv_path)
            typer.echo(f"Exported {len(jobs)} jobs to {csv_path}")
        except Exception as e:
            typer.echo(f"Error exporting CSV: {e}", err=True)
            raise typer.Exit(code=1)


@app.command("skills")
def skills(
    start_date: str = typer.Argument(..., help="Start date (YYYY-MM-DD)"),
    end_date: str = typer.Argument(..., help="End date (YYYY-MM-DD)"),
    limit: int = typer.Option(1000, "--limit", "-l", help="Max jobs to scan (default:1000)"),
    csv_path: Optional[str] = typer.Option(None, "--csv", help="Optional CSV export path"),
):
    """Count occurrences of skills in job descriptions between two dates.

    Example: `python jobscli.py skills 2025-01-01 2025-06-30 --limit 1000`
    """
    # default skill list; you can extend as needed
    skills_list = [
        "python",
        "java",
        "javascript",
        "c#",
        "php",
        "sql",
        "aws",
        "docker",
        "kubernetes",
        "react",
        "angular",
    ]

    try:
        s_date = date.fromisoformat(start_date)
        e_date = date.fromisoformat(end_date)
    except Exception as e:
        typer.echo(f"Invalid date format: {e}", err=True)
        raise typer.Exit(code=1)

    try:
        resp = list_jobs(limit)
        data = resp.json()
        jobs = _extract_jobs_from_response(data)
    except Exception as e:
        typer.echo(f"Error fetching jobs: {e}", err=True)
        raise typer.Exit(code=1)

    # filter by publication date (try to find YYYY-MM-DD in job data)
    matched_jobs = []
    counts: Dict[str, int] = {k: 0 for k in skills_list}

    date_re = re.compile(r"(\d{4}-\d{2}-\d{2})")
    for job in jobs:
        text = json.dumps(job).lower()
        m = date_re.search(text)
        if not m:
            continue
        try:
            jd = date.fromisoformat(m.group(1))
        except Exception:
            continue

        if not (s_date <= jd <= e_date):
            continue

        # job falls in date range; count skills
        content = (str(job.get("title", "")) + " " + str(job.get("description", ""))).lower()
        found = False
        for skill in skills_list:
            # word boundary matching
            matches = re.findall(r"\b" + re.escape(skill) + r"\b", content)
            if matches:
                counts[skill] += len(matches)
                found = True

        if found:
            matched_jobs.append(job)

    # sort counts descending and produce a single dict inside a list as requested
    ordered = dict(sorted({k: v for k, v in counts.items() if v > 0}.items(), key=lambda x: x[1], reverse=True))

    typer.echo(json.dumps([ordered], indent=2, ensure_ascii=False))

    if csv_path:
        try:
            _export_to_csv(matched_jobs, csv_path)
            typer.echo(f"Exported {len(matched_jobs)} matched jobs to {csv_path}")
        except Exception as e:
            typer.echo(f"Error exporting CSV: {e}", err=True)
            raise typer.Exit(code=1)

@list_app.command("skills")
def list_skills(
    role: str = typer.Argument(..., help='Job role to search (e.g. "data scientist")'),
    top: int = typer.Option(10, "--top", "-t", help="Top N skills (default: 10)"),
    pages: int = typer.Option(3, "--pages", "-p", help="How many Teamlyzer pages to scan (default: 3)"),
):
    """
    Scrape Teamlyzer jobs and return top skills (stack) for a given role as JSON.
    Example:
      python jobscli.py list skills "data scientist"
    """
    role = (role or "").strip()
    if not role:
        typer.echo("Role is required.", err=True)
        raise typer.Exit(code=1)

    counts: Dict[str, int] = {}

    try:
        for page in range(1, pages + 1):
            url = _teamlyzer_jobs_search_url(role, page=page)
            r = requests.get(url, headers=TEAMLYZER_UA, timeout=20)
            r.raise_for_status()

            tags = _extract_skill_tags_from_teamlyzer_jobs_html(r.text)
            if not tags and page == 1:
                # no tags at all; still output empty JSON list
                break

            for tag in tags:
                counts[tag] = counts.get(tag, 0) + 1

        # top N
        items = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:top]
        out = [{"skill": k, "count": v} for k, v in items]

        typer.echo(json.dumps(out, indent=2, ensure_ascii=False))

    except Exception as e:
        typer.echo(f"Error scraping Teamlyzer skills: {e}", err=True)
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()