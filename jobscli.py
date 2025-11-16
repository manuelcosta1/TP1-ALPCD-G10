from urllib.parse import urljoin
from typing import Dict, Any, Optional, List
import requests
import typer
import json
import re
import csv
from datetime import date


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


app = typer.Typer()


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

if __name__ == "__main__":
    app()
