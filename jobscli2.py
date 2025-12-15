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

# CONFIGURAÇÃO

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

TEAMLYZER_UA = {"User-Agent": "Mozilla/5.0 (compatible; TeamlyzerScraper/1.0)"}
TEAMLYZER_BASE = "https://pt.teamlyzer.com"

# A app Typer tem de existir antes dos decorators @app.command(...)
app = typer.Typer()

# HELPERS (API ITJOBS)

def _get_api_key() -> str:
    return API_CONFIG["api"].get("key", "")


def _build_url(name: str) -> str:
    api = API_CONFIG["api"]
    endpoints = api.get("endpoints", {})
    if name not in endpoints:
        raise KeyError(f"Endpoint desconhecido: {name}")

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


def search_jobs(q: str, limit: int, extra_params: Optional[Dict[str, Any]] = None):
    """Chama o endpoint `search` com a query `q` e devolve um `requests.Response`."""
    params = _get_params(extra_params)
    params["limit"] = limit
    params["q"] = q

    headers = _get_headers()

    url = _build_url("search")
    resp = requests.get(url, params=params, headers=headers)
    resp.raise_for_status()

    return resp


def get_job(job_id: str):
    """Chama o endpoint `get` para obter detalhes de uma vaga pelo ID."""
    params = _get_params({"id": job_id})
    headers = _get_headers()

    url = _build_url("get")
    resp = requests.get(url, params=params, headers=headers)
    resp.raise_for_status()

    return resp

# HELPERS (TEAMLYZER / NORMALIZAÇÃO / HTML)

def _norm(s: str) -> str:
    """Normaliza string para matching mais flexível (minúsculas, sem acentos, trims, espaços)."""
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"\s+", " ", s)
    return s


def _html_to_text(html: str) -> str:
    """Conversão muito simples de HTML -> texto."""
    html = unescape(html or "")
    # remover scripts/styles
    html = re.sub(r"(?is)<script.*?>.*?</script>", " ", html)
    html = re.sub(r"(?is)<style.*?>.*?</style>", " ", html)
    # substituir <br> e </p> por quebras de linha
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</p\s*>", "\n", html)
    # remover tags
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    # normalizar espaços
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def _extract_company_name(job_json: Dict[str, Any]) -> str:
    """Tenta (de forma robusta) extrair o nome da empresa do payload de uma vaga do itjobs."""
    if not isinstance(job_json, dict):
        return ""
    # padrões comuns em APIs
    for key in ("company", "empresa"):
        val = job_json.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, dict):
            for k2 in ("name", "nome", "title"):
                v2 = val.get(k2)
                if isinstance(v2, str) and v2.strip():
                    return v2.strip()

    # fallback: algumas APIs usam "company_name"
    for key in ("company_name", "companyName", "empresa_nome"):
        v = job_json.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()

    return ""


def _find_teamlyzer_company_slug(company_name: str, max_pages_fallback: int = 3) -> Optional[str]:
    """
    Procura o slug da empresa no Teamlyzer.
    1) Tenta /companies/ranking (Top 50)
    2) Fallback: tenta /companies/ (primeiras N páginas)
    Devolve slug como "blip" ou "pwc".
    """
    target = _norm(company_name)
    if not target:
        return None

    # helper: extrair pares (slug, display_name) do HTML
    def extract_pairs(html: str) -> List[tuple]:
        pairs = []
        # links de perfil costumam ser /companies/<slug>
        for m in re.finditer(r'href="(/companies/([a-z0-9\-]+))"', html, flags=re.I):
            slug = m.group(2)
            # evitar páginas óbvias que não são empresas
            if slug in {"ranking", "awards", "jobs", "remote-companies"}:
                continue
            pairs.append((slug, slug))
        # remover duplicados mantendo ordem
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
        # a página de ranking tem nomes visíveis; fazemos texto simples e comparamos
        text = _html_to_text(html)
        # brute: se o nome aparece, tentar achar slug por heurística
        if target in _norm(text):
            for slug, _ in extract_pairs(html):
                if slug and slug in _norm(company_name).replace(" ", "-"):
                    return slug
        # heurística leve: "Blip.pt" -> "blip" (remover pontuação)
        guess = re.sub(r"[^a-z0-9\-]+", "", _norm(company_name).replace(" ", "-"))
        if guess:
            if re.search(rf'href="/companies/{re.escape(guess)}"', html, flags=re.I):
                return guess
    except Exception:
        pass

    # 2) fallback: varrer primeiras N páginas de /companies/
    for page in range(1, max_pages_fallback + 1):
        try:
            url = f"{TEAMLYZER_BASE}/companies/"
            if page != 1:
                url = f"{TEAMLYZER_BASE}/companies/?page={page}"
            r = requests.get(url, headers=TEAMLYZER_UA, timeout=20)
            r.raise_for_status()
            html = r.text
            text = _html_to_text(html)

            if target in _norm(text):
                guess = re.sub(r"[^a-z0-9\-]+", "", _norm(company_name).replace(" ", "-"))
                if guess and re.search(rf'href="/companies/{re.escape(guess)}"', html, flags=re.I):
                    return guess

                # tentar slug que apareça no nome normalizado
                for slug, _ in extract_pairs(html):
                    if slug and slug in _norm(company_name).replace(" ", "-"):
                        return slug

                # último recurso: procurar slug “perto” do nome (heurística fraca)
                for slug, _ in extract_pairs(html):
                    if slug and re.search(rf"\b{re.escape(slug)}\b", _norm(company_name).replace(" ", "-")):
                        return slug
        except Exception:
            continue

    return None


def _scrape_teamlyzer_company(slug: str, top_benefits: int = 5) -> Dict[str, Any]:
    """
    Faz scraping do rating, descrição, salário e benefícios a partir das páginas da empresa no Teamlyzer.
    """
    out: Dict[str, Any] = {
        "teamlyzer_rating": None,
        "teamlyzer_description": None,
        "teamlyzer_benefits": [],
        "teamlyzer_salary": None,
    }

    if not slug:
        return out

    # página principal da empresa
    try:
        r = requests.get(f"{TEAMLYZER_BASE}/companies/{slug}", headers=TEAMLYZER_UA, timeout=20)
        r.raise_for_status()
        text = _html_to_text(r.text)

        # rating: primeiro match do tipo 3.4/5
        m = re.search(r"(\d(?:\.\d)?)\s*/\s*5", text)
        if m:
            try:
                out["teamlyzer_rating"] = float(m.group(1))
            except Exception:
                out["teamlyzer_rating"] = m.group(1)

        # descrição: heurística simples (primeira linha “grande” no topo que não seja menu/metadata)
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        desc = None
        for ln in lines[:40]:
            if len(ln) >= 60 and not re.search(r"/5|Reviews|Visão geral|Emprego|Entrevista|Salário|Seguir", ln):
                desc = ln
                break
        if desc:
            out["teamlyzer_description"] = desc

        # salário: tentar apanhar "salário médio ... entre os X€ e Y€."
        ms = re.search(
            r"sal[aá]rio m[eé]dio.*?entre\s+os\s+([^\.]+?)\s+e\s+([^\.]+?)\.",
            text,
            flags=re.I
        )
        if ms:
            out["teamlyzer_salary"] = f"entre {ms.group(1).strip()} e {ms.group(2).strip()}"
    except Exception:
        pass

    # página de benefícios (benefits-and-values)
    try:
        r = requests.get(
            f"{TEAMLYZER_BASE}/companies/{slug}/benefits-and-values",
            headers=TEAMLYZER_UA,
            timeout=20
        )
        r.raise_for_status()
        text = _html_to_text(r.text)

        # extrair linhas tipo bullet sob "Benefícios e vantagens"
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
                if len(title) >= 3:
                    benefits.append(title)

        # manter únicos, top N
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
    """Devolve uma cópia de job_json enriquecida com campos do Teamlyzer."""
    if not isinstance(job_json, dict):
        return {"data": job_json}

    company_name = _extract_company_name(job_json)
    slug = _find_teamlyzer_company_slug(company_name, max_pages_fallback=fallback_pages) if company_name else None
    info = _scrape_teamlyzer_company(slug or "", top_benefits=5)

    enriched = dict(job_json)
    enriched.update(info)
    return enriched

# HELPERS (EXTRAÇÃO / CSV / LÓGICA)

def top_jobs(limit: int) -> list:
    resp = list_jobs(limit)
    data = resp.json()
    jobs = _extract_jobs_from_response(data)
    return jobs


def extract_work_regime(job_data: Dict[str, Any]) -> str:
    """Extrai o regime de trabalho (remoto/híbrido/presencial/outro) a partir dos dados da vaga."""
    text = json.dumps(job_data).lower()

    if re.search(r"\bremoto\b|\bremote\b", text):
        return "remote"
    elif re.search(r"\bh[íi]brido\b|\bhybrid\b", text):
        return "hybrid"
    elif re.search(r"\bpresencial\b|\bon-?site\b|\bfísico\b", text):
        return "on-site"
    else:
        return "other"


def _extract_jobs_from_response(data: Any) -> List[Dict[str, Any]]:
    """Tenta encontrar e devolver a lista de vagas a partir da resposta da API."""
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in ("jobs", "results", "data", "items"):
            val = data.get(key)
            if isinstance(val, list):
                return val

        for val in data.values():
            if isinstance(val, list):
                return val

    return []


def _pick_job_field(job: Dict[str, Any], *keys: str) -> str:
    """Tenta várias chaves e devolve a primeira string não vazia."""
    for k in keys:
        v = job.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):
            for kk in ("name", "title", "nome"):
                vv = v.get(kk)
                if isinstance(vv, str) and vv.strip():
                    return vv.strip()
    return ""


def _normalize_job_for_csv(job: Dict[str, Any]) -> Dict[str, Any]:
    """Extrai campos para CSV de forma segura.

    Campos: titulo, empresa, descricao, data_de_publicacao, salario, localizacao
    """
    def pick(obj: Dict[str, Any], *keys):
        for k in keys:
            v = obj.get(k)
            if v:
                return v
        return ""

    title = pick(job, "title", "titulo", "job_title")
    company = pick(job, "company", "empresa", "company_name")
    description = pick(job, "description", "descricao", "job_description")
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
    """Escreve uma lista de vagas para CSV em `path` (campos normalizados)."""
    if not jobs:
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


def _export_single_job_enriched_to_csv(job: Dict[str, Any], path: str) -> None:
    headers = [
        "id",
        "titulo",
        "empresa",
        "localizacao",
        "tipo",
        "data_de_publicacao",
        "teamlyzer_rating",
        "teamlyzer_description",
        "teamlyzer_salary",
        "teamlyzer_benefits",
    ]

    row = {
        "id": str(job.get("id", "")),
        "titulo": str(job.get("title", job.get("titulo", ""))),
        "empresa": str(_extract_company_name(job)),
        "localizacao": str(job.get("location", "")),
        "tipo": str(job.get("type", "")),
        "data_de_publicacao": str(job.get("date", job.get("published_at", ""))),
        "teamlyzer_rating": str(job.get("teamlyzer_rating", "")),
        "teamlyzer_description": str(job.get("teamlyzer_description", "")),
        "teamlyzer_salary": str(job.get("teamlyzer_salary", "")),
        "teamlyzer_benefits": "; ".join(job.get("teamlyzer_benefits", []) or []),
    }

    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=headers,
            delimiter=";",  
        )
        writer.writeheader()
        writer.writerow(row)


def _extract_zone_from_job(job: Dict[str, Any]) -> str:
    # No detalhe, o itjobs costuma ter locations como lista de dicts
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
    # No detalhe, o tipo pode vir em vários formatos
    for k in ("type", "tipo", "employment_type", "contract_type", "contract"):
        v = job.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()

    # Às vezes vem como lista (ex.: types)
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

# COMANDOS CLI 

@app.command("top")
def top(
    limit: int = typer.Argument(..., help="Número de vagas a obter"),
    csv_path: Optional[str] = typer.Option(None, "--csv", help="Caminho do CSV para exportar resultados"),
):
    """Lista as N vagas mais recentes e imprime um array JSON no stdout.

    Uso: `python jobscli.py top 30` ou `python jobscli.py top 30 --csv out.csv`
    """
    try:
        jobs = top_jobs(limit)
    except Exception as e:
        typer.echo(f"Erro ao obter vagas: {e}", err=True)
        raise typer.Exit(code=1)

    typer.echo(json.dumps(jobs, indent=2, ensure_ascii=False))

    if csv_path:
        try:
            _export_to_csv(jobs, csv_path)
            typer.echo(f"Exportadas {len(jobs)} vagas para {csv_path}")
        except Exception as e:
            typer.echo(f"Erro ao exportar CSV: {e}", err=True)
            raise typer.Exit(code=1)


@app.command("search")
def search(
    q: str = typer.Argument(..., help="Query de pesquisa (obrigatória)"),
    limit: int = typer.Option(10, "--limit", "-l", help="Número de resultados (por omissão: 10)"),
    company: str = typer.Option(None, "--company", "-c", help="Filtrar por nome da empresa"),
    type_: str = typer.Option(None, "--type", "-t", help="Filtrar por tipo de vaga"),
    contract: str = typer.Option(None, "--contract", help="Filtrar por tipo de contrato"),
    page: int = typer.Option(1, "--page", "-p", help="Número da página (por omissão: 1)"),
):
    """Pesquisa vagas por query e filtros opcionais.

    Uso CLI: `python jobscli.py search "QUERY" [--limit 20] [--company "X"] [--type "full-time"] [--contract "permanent"] [--page 2]`
    """
    try:
        # Construir parâmetros extra com base nos filtros opcionais
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
        typer.echo(f"Erro ao pesquisar: {e}", err=True)
        raise typer.Exit(code=1)

    typer.echo(json.dumps(data, indent=2, ensure_ascii=False))


@app.command("type")
def work_type(job_id: str = typer.Argument(..., help="ID da vaga (obrigatório)")):
    """Extrai o regime de trabalho de uma vaga.

    Uso CLI: `python jobscli.py type 12345`
    """
    try:
        resp = get_job(job_id)
        data = resp.json()
        regime = extract_work_regime(data)
        typer.echo(regime)
    except Exception as e:
        typer.echo(f"Erro ao obter a vaga: {e}", err=True)
        raise typer.Exit(code=1)


@app.command("list-company")
def list_company(
    location: str = typer.Argument(..., help="Localização (obrigatória)"),
    company: str = typer.Argument(..., help="Empresa (obrigatória)"),
    limit: int = typer.Argument(50, help="Número de vagas a obter (obrigatório)"),
    csv_path: Optional[str] = typer.Option(None, "--csv", help="Caminho do CSV para exportar"),
):
    """Lista vagas part-time de uma empresa numa localização específica.

    Uso CLI: `python jobscli.py list-company Porto EmpresaY 3 [--csv out.csv]`
    """
    try:
        extra = {
            "type": "part-time",
            "company": company,
            "location": location,
        }
        # Usar company como query para aumentar probabilidade de resultados relevantes
        resp = search_jobs(company, limit=limit, extra_params=extra)
        data = resp.json()
        jobs = _extract_jobs_from_response(data)
    except Exception as e:
        typer.echo(f"Erro ao obter vagas: {e}", err=True)
        raise typer.Exit(code=1)

    typer.echo(json.dumps(jobs, indent=2, ensure_ascii=False))

    if csv_path:
        try:
            _export_to_csv(jobs, csv_path)
            typer.echo(f"Exportadas {len(jobs)} vagas para {csv_path}")
        except Exception as e:
            typer.echo(f"Erro ao exportar CSV: {e}", err=True)
            raise typer.Exit(code=1)


@app.command("skills")
def skills(
    start_date: str = typer.Argument(..., help="Data inicial (YYYY-MM-DD)"),
    end_date: str = typer.Argument(..., help="Data final (YYYY-MM-DD)"),
    limit: int = typer.Option(1000, "--limit", "-l", help="Máx. vagas a analisar (por omissão: 1000)"),
    csv_path: Optional[str] = typer.Option(None, "--csv", help="Exportação opcional para CSV"),
):
    """Conta ocorrências de skills em descrições de vagas entre duas datas.

    Exemplo: `python jobscli.py skills 2025-01-01 2025-06-30 --limit 1000`
    """
    # Lista de skills por omissão (podes estender)
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
        typer.echo(f"Formato de data inválido: {e}", err=True)
        raise typer.Exit(code=1)

    try:
        resp = list_jobs(limit)
        data = resp.json()
        jobs = _extract_jobs_from_response(data)
    except Exception as e:
        typer.echo(f"Erro ao obter vagas: {e}", err=True)
        raise typer.Exit(code=1)

    # filtrar por data de publicação (tentar encontrar YYYY-MM-DD em job)
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

        # vaga dentro do intervalo: contar skills
        content = (str(job.get("title", "")) + " " + str(job.get("description", ""))).lower()
        found = False
        for skill in skills_list:
            # matching com word boundaries
            matches = re.findall(r"\b" + re.escape(skill) + r"\b", content)
            if matches:
                counts[skill] += len(matches)
                found = True

        if found:
            matched_jobs.append(job)

    # ordenar desc e devolver dict único dentro de lista, como pedido
    ordered = dict(
        sorted(
            {k: v for k, v in counts.items() if v > 0}.items(),
            key=lambda x: x[1],
            reverse=True
        )
    )

    typer.echo(json.dumps([ordered], indent=2, ensure_ascii=False))

    if csv_path:
        try:
            _export_to_csv(matched_jobs, csv_path)
            typer.echo(f"Exportadas {len(matched_jobs)} vagas correspondentes para {csv_path}")
        except Exception as e:
            typer.echo(f"Erro ao exportar CSV: {e}", err=True)
            raise typer.Exit(code=1)


@app.command("get")
def get(
    job_id: str = typer.Argument(..., help="ID da vaga (obrigatório)"),
    teamlyzer_pages: int = typer.Option(
        3,
        "--teamlyzer-pages",
        help="Quantas páginas de /companies varrer se não encontrar no ranking (por omissão: 3)",
    ),
    csv_path: Optional[str] = typer.Option(
        None, "--csv", help="Exportação opcional para CSV"
    ),
):
    """Obtém detalhes da vaga por ID e enriquece com info da empresa no Teamlyzer.

    Exemplo:
      python jobscli.py get 125378
    """
    try:
        resp = get_job(job_id)
        data = resp.json()

        # A API às vezes devolve wrapper; tentar localizar o dict da vaga
        job_obj = data
        if isinstance(data, dict):
            for k in ("job", "data", "result"):
                if isinstance(data.get(k), dict):
                    job_obj = data[k]
                    break

        enriched = enrich_job_with_teamlyzer(job_obj, fallback_pages=teamlyzer_pages)
        typer.echo(json.dumps(enriched, indent=2, ensure_ascii=False))

        # exportar para CSV (alínea d)
        if csv_path:
            _export_single_job_enriched_to_csv(enriched, csv_path)
            typer.echo(f"Exportada 1 vaga para {csv_path}")

    except Exception as e:
        typer.echo(f"Erro ao obter/enriquecer vaga: {e}", err=True)
        raise typer.Exit(code=1)


@app.command("statistics")
def statistics(
    group: str = typer.Argument(..., help="Modo de agrupamento (usar: zone)"),
    limit: int = typer.Option(200, "--limit", "-l", help="Máx. vagas a analisar (por omissão: 200)"),
    out: str = typer.Option("statistics.csv", "--out", "-o", help="Caminho do CSV de saída"),
):
    """
    Cria estatísticas em CSV contando vagas por Zona e Tipo de Trabalho.
    Usa list.json para obter IDs e get.json para extrair campos de forma fiável.
    """
    group = (group or "").strip().lower()
    if group != "zone":
        typer.echo("Neste trabalho, usa: statistics zone", err=True)
        raise typer.Exit(code=1)

    # 1) obter lista (IDs)
    try:
        resp = list_jobs(limit)
        data = resp.json()
        jobs = _extract_jobs_from_response(data)
    except Exception as e:
        typer.echo(f"Erro ao obter lista de vagas: {e}", err=True)
        raise typer.Exit(code=1)

    counts: Dict[tuple, int] = {}

    # 2) para cada vaga, obter detalhe (get.json) e extrair zona/tipo
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
            # se falhar uma vaga, ignorar e continuar
            continue

    # 3) exportar CSV
    try:
        with open(out, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh,delimiter=";")
            writer.writerow(["Zona", "Tipo de Trabalho", "Nº de vagas"])

            for (zone, job_type), n in sorted(
                counts.items(),
                key=lambda kv: (kv[0][0].lower(), -kv[1], kv[0][1].lower()),
            ):
                writer.writerow([zone, job_type, n])
            

        typer.echo("Ficheiro de exportação criado com sucesso.")
    except Exception as e:
        typer.echo(f"Erro ao escrever CSV: {e}", err=True)
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
