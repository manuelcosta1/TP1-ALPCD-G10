from urllib.parse import urljoin
from typing import Dict, Any, Optional
import requests
import typer
import json
import re


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


@app.command("top")
def top(limit: int = typer.Argument(..., help="Number of jobs to fetch")):
    
    try:
        resp = list_jobs(limit)
        data = resp.json()
    except Exception as e:
        typer.echo(f"Error fetching jobs: {e}", err=True)
        raise typer.Exit(code=1)

    typer.echo(json.dumps(data, indent=2, ensure_ascii=False))


def search_jobs(q: str, limit: int, extra_params: Optional[Dict[str, Any]] = None):

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

    params = _get_params({"id": job_id})
    headers = _get_headers()
    
    url = _build_url("get")
    resp = requests.get(url, params=params, headers=headers)
    resp.raise_for_status()
    
    return resp


def extract_work_regime(job_data: Dict[str, Any]) -> str:

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


@app.command("type")
def work_type(job_id: str = typer.Argument(..., help="Job ID (required)")):
    
    try:
        resp = get_job(job_id)
        data = resp.json()
        regime = extract_work_regime(data)
        typer.echo(regime)
    except Exception as e:
        typer.echo(f"Error fetching job: {e}", err=True)
        raise typer.Exit(code=1)

@app.command("who")
def version():
    """Print the CLI version."""
    typer.echo("""
            ╔═══════════════════════════╗
                     Feito por:
                      Gabriel 
                      Manuel
                      Rodrigo
            ╚═══════════════════════════╝
               """)

if __name__ == "__main__":
    app()


