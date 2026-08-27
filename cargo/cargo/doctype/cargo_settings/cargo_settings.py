# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class CargoSettings(Document):
	"""Where Cargo reaches Atlas and Central, and the key it stamps onto machines.

	Shared by every service, so owned by none of them.
	"""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		atlas_token: DF.Password
		atlas_url: DF.Data
		central_token: DF.Password
		central_url: DF.Data
		ssh_public_key: DF.SmallText | None
	# end: auto-generated types
