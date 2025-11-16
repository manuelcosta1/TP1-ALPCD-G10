import typer
from itjobs_api import get_top_jobs, search_part_time_jobs, get_job_by_id
from utils import export_jobs_to_csv
from skills import count_skills_between_dates

app = typer.Typer()

@app.command()
def top(n: int, csv: bool = False, csv_file: str = "top_jobs.csv"):
    """Lista os N trabalhos mais recentes."""
    jobs = get_top_jobs(n)
    # imprimir JSON
    import json
    print(json.dumps(jobs, ensure_ascii=False, indent=2))
    if csv:
        export_jobs_to_csv(jobs, csv_file)

# (vais adicionar os outros comandos aqui)

if __name__ == "__main__":
    app()
