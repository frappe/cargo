import click
import frappe
from frappe.commands import pass_context
from frappe.exceptions import SiteNotSpecifiedError
from frappe.utils.bench_helper import CliCtxObj


@click.command("enable-pilot-release-tracker")
@pass_context
def enable_pilot_release_tracker(context: CliCtxObj) -> None:
	"""Build images for new Pilot prereleases."""
	set_pilot_release_tracking(context, enabled=True)


@click.command("disable-pilot-release-tracker")
@pass_context
def disable_pilot_release_tracker(context: CliCtxObj) -> None:
	"""Stop building images for new Pilot prereleases."""
	set_pilot_release_tracking(context, enabled=False)


def set_pilot_release_tracking(context: CliCtxObj, *, enabled: bool) -> None:
	"""Set release tracking for every selected site."""
	if not context.sites:
		raise SiteNotSpecifiedError

	for site in context.sites:
		try:
			frappe.init(site)
			frappe.connect()
			frappe.db.set_single_value("Cargo Settings", "track_pilot_releases", enabled)
			frappe.db.commit()  # nosemgrep
			click.echo(f"Pilot release tracking {'enabled' if enabled else 'disabled'} on {site}")
		finally:
			frappe.destroy()


commands = [enable_pilot_release_tracker, disable_pilot_release_tracker]
