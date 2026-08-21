"""Command line interface."""

from __future__ import annotations

import csv
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import select

from ght.config import settings
from ght.db import create_all, session_scope
from ght.models import AccessLog, Account, CollectionRun, Evidence, Site
from ght.pipeline.evidence import verify
from ght.pipeline.run import run_site
from ght.sources import load_source, scan_sources

app = typer.Typer(help="Collect published gambling-site deposit accounts for AML review.")
console = Console()


@app.command("init-db")
def init_db() -> None:
    """Create the database schema (use Alembic once migrations are in play)."""
    create_all()
    console.print(f"[green]schema ready[/green] at {settings.database_url}")


def _report_broken(broken) -> None:
    """Name the config files that would not parse, so they cannot fail silently."""
    for entry in broken:
        console.print(f"[red]broken config[/red] {entry.path.name}: {entry.error}")


@app.command("sites")
def list_sites() -> None:
    """List the configured target sites."""
    configs, broken = scan_sources()
    table = Table("slug", "name", "status", "fetcher", "blocks", "urls")
    for config in configs:
        table.add_row(
            config.slug,
            config.name,
            config.status,
            config.fetcher,
            str(len(config.blocks)),
            str(len(config.urls)),
        )
    console.print(table)
    _report_broken(broken)


@app.command("run")
def run(
    site: str | None = typer.Option(None, "--site", "-s", help="Slug; omit to run all sites."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Extract and print, write nothing."),
    progress: bool = typer.Option(
        False, "--progress", help="Emit machine-readable step updates (used by the portal)."
    ),
) -> None:
    """Collect from one site or from every active site."""
    from ght.api.jobs import claim_run_lock, release_run_lock
    from ght.progress import emit_to_stdout

    on_progress = emit_to_stdout if progress else None

    # One collection on the machine at a time. Two of them race on the same login session
    # and double the traffic to a site that is already watching for it. This matters more
    # now that the portal can collect on a schedule: the other one may be a run nobody is
    # sitting in front of. A dry run reads and writes nothing, so it is not gated.
    held = None if dry_run else claim_run_lock(site or "all sites")
    if held is not None:
        console.print(
            f"[yellow]another collection is already running[/yellow] "
            f"({held.get('slug', 'unknown')}, process {held.get('pid')})"
        )
        raise typer.Exit(1)

    if site:
        configs = [load_source(site)]
    else:
        all_configs, broken = scan_sources()
        # Reported before the run so a typo is visible even if the rest collects cleanly.
        _report_broken(broken)
        configs = [c for c in all_configs if c.status == "active"]

    try:
        _collect(configs, dry_run=dry_run, on_progress=on_progress)
    finally:
        if not dry_run:
            release_run_lock()


def _collect(configs, dry_run: bool, on_progress) -> None:
    for config in configs:
        with session_scope() as session:
            report = run_site(session, config, dry_run=dry_run, on_progress=on_progress)

            colour = {"ok": "green", "partial": "yellow"}.get(report.status, "red")
            console.print(
                f"[{colour}]{report.status:8}[/{colour}] {config.slug:20} "
                f"accounts={report.account_count} new={len(report.changes.new_account_ids)} "
                f"gone={len(report.changes.disappeared_account_ids)}"
                + (f" error={report.error}" if report.error else "")
            )

            if report.extraction and report.extraction.extractor_looks_broken:
                console.print(
                    f"  [yellow]selectors matched nothing but the page still has "
                    f"{report.extraction.sweep_hits} numbers - config is stale[/yellow]"
                )

            if dry_run and report.extraction:
                for account in report.extraction.accounts:
                    console.print(
                        f"  {account.channel:14} {account.account_number:20} "
                        f"conf={account.confidence} {account.bank_name or ''}"
                    )


@app.command("accounts")
def list_accounts(
    active_only: bool = typer.Option(True, "--active/--all"),
    channel: str | None = typer.Option(None, "--channel", "-c"),
    review: bool = typer.Option(False, "--review", help="Only accounts awaiting review."),
    limit: int = typer.Option(50, "--limit", "-n"),
) -> None:
    """List collected accounts."""
    with session_scope() as session:
        query = select(Account).order_by(Account.last_seen_at.desc())
        if active_only:
            query = query.where(Account.is_active.is_(True))
        if channel:
            query = query.where(Account.channel == channel)
        if review:
            query = query.where(Account.needs_review.is_(True))

        rows = list(session.scalars(query.limit(limit)))
        table = Table("channel", "number", "type", "bank", "sites", "seen", "first", "last")
        # The account number is the whole point of the table, so it never gets wrapped or
        # truncated to fit a narrow terminal; the bank name gives way instead.
        table.columns[1].no_wrap = True
        table.columns[3].overflow = "ellipsis"
        for account in rows:
            table.add_row(
                account.channel,
                account.account_number,
                account.account_type or "-",
                account.bank_name or "-",
                str(len(account.site_links)),
                str(account.observation_count),
                account.first_seen_at.strftime("%m-%d %H:%M"),
                account.last_seen_at.strftime("%m-%d %H:%M"),
            )
        console.print(table)
        console.print(f"{len(rows)} account(s)")


@app.command("export")
def export(
    out: Path = typer.Option(Path("accounts.csv"), "--out", "-o"),
    active_only: bool = typer.Option(True, "--active/--all"),
    reviewed_only: bool = typer.Option(
        True,
        "--reviewed/--include-unreviewed",
        help="Exclude low-confidence hits that no selector vouched for.",
    ),
    actor: str = typer.Option("cli", "--actor", help="Recorded in the access log."),
) -> None:
    """Export accounts to CSV for the AML team."""
    with session_scope() as session:
        query = select(Account).order_by(Account.channel, Account.account_number)
        if active_only:
            query = query.where(Account.is_active.is_(True))
        if reviewed_only:
            query = query.where(Account.needs_review.is_(False))

        rows = list(session.scalars(query))
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                [
                    "channel",
                    "account_number",
                    "account_type",
                    "bank_name",
                    "branch",
                    "holder_name",
                    "operator",
                    "sites",
                    "observations",
                    "confidence",
                    "first_seen_utc",
                    "last_seen_utc",
                ]
            )
            for account in rows:
                writer.writerow(
                    [
                        account.channel,
                        account.account_number,
                        account.account_type or "",
                        account.bank_name or "",
                        account.branch or "",
                        account.holder_name or "",
                        account.operator or "",
                        len(account.site_links),
                        account.observation_count,
                        account.confidence,
                        account.first_seen_at.isoformat(),
                        account.last_seen_at.isoformat(),
                    ]
                )

        # Who exported the account data, and how much of it, is itself auditable.
        session.add(
            AccessLog(
                actor=actor,
                action="export",
                params={"active_only": active_only, "reviewed_only": reviewed_only},
                row_count=len(rows),
            )
        )
    console.print(f"[green]wrote {len(rows)} account(s)[/green] to {out}")


@app.command("verify-evidence")
def verify_evidence(
    limit: int = typer.Option(0, "--limit", "-n", help="0 checks everything."),
) -> None:
    """Re-hash stored evidence blobs and report any that no longer match."""
    with session_scope() as session:
        query = select(Evidence).order_by(Evidence.id.desc())
        if limit:
            query = query.limit(limit)

        checked = failed = 0
        for blob in session.scalars(query):
            checked += 1
            if not verify(blob.path, blob.sha256):
                failed += 1
                console.print(f"[red]MISMATCH[/red] {blob.path}")

    colour = "red" if failed else "green"
    console.print(f"[{colour}]{checked - failed}/{checked} blobs verified[/{colour}]")


@app.command("status")
def status(limit: int = typer.Option(15, "--limit", "-n")) -> None:
    """Show the most recent collection runs."""
    with session_scope() as session:
        table = Table("started", "site", "status", "http", "candidates", "new", "error")
        query = (
            select(CollectionRun, Site)
            .join(Site, Site.id == CollectionRun.site_id)
            .order_by(CollectionRun.id.desc())
            .limit(limit)
        )
        for run_row, site in session.execute(query):
            colour = {"ok": "green", "partial": "yellow"}.get(run_row.status, "red")
            table.add_row(
                run_row.started_at.strftime("%m-%d %H:%M"),
                site.slug,
                f"[{colour}]{run_row.status}[/{colour}]",
                str(run_row.http_status or "-"),
                str(run_row.candidates_found),
                str(run_row.accounts_new),
                (run_row.error or "")[:40],
            )
        console.print(table)


@app.command("serve")
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address."),
    port: int = typer.Option(8000, "--port", "-p"),
    reload: bool = typer.Option(False, "--reload", help="Restart on code changes."),
) -> None:
    """Run the read-only web portal over the collected accounts."""
    try:
        import uvicorn
    except ImportError:
        console.print('[red]the api extra is not installed[/red]: pip install -e ".[api]"')
        raise typer.Exit(1) from None

    # Bound to loopback by default on purpose: the portal has no authentication, and this
    # database is a list of accounts under investigation.
    console.print(f"portal on [cyan]http://{host}:{port}[/cyan]  (ctrl-c to stop)")
    uvicorn.run("ght.api:create_app", host=host, port=port, reload=reload, factory=True)


if __name__ == "__main__":
    app()
