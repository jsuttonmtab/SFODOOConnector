# -*- coding: utf-8 -*-
import logging

from odoo import fields, models

_logger = logging.getLogger(__name__)


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    sf_line_id = fields.Char(
        string='Salesforce Line ID',
        index=True,
        copy=False,
        help='Salesforce OpportunityLineItem Id. Upsert key per line.',
    )
    sf_data_vendor = fields.Char(
        string='Data Vendor',
    )
    sf_coverage_period = fields.Char(
        string='Coverage Period',
    )
    sf_study_type = fields.Char(
        string='Study Type',
    )
    sf_study_name = fields.Char(
        string='Study Name',
    )
    sf_wave_period = fields.Char(
        string='Wave Period',
    )
    sf_delivery = fields.Selection(
        selection=[
            ('classic', 'Classic'),
            ('halo', 'Halo'),
            ('both', 'Both'),
        ],
        string='Delivery',
    )
    sf_information_only = fields.Boolean(
        string='Information Only',
    )
    sf_is_upsell = fields.Boolean(
        string='Upsell',
    )
    sf_on_hold = fields.Boolean(
        string='On Hold',
    )
    sf_removed_in_sf = fields.Boolean(
        string='Removed in Salesforce',
        help='Line was removed in Salesforce. Kept at quantity 0 as a change log.',
    )
    sf_term_start_date = fields.Date(
        string='Term Start Date',
    )
    sf_term_end_date = fields.Date(
        string='Term End Date',
    )
    # Stored as-is. Odoo does not generate invoices from this value.
    sf_billing_schedule = fields.Char(
        string='Billing Schedule',
    )

    _sf_line_id_unique = models.Constraint(
        'UNIQUE(sf_line_id)',
        'Another order line already has this Salesforce Line ID.',
    )

    def write(self, vals):
        # Only lines whose hold flag actually changes need their deliverables updated.
        changed = self.browse()
        if 'sf_on_hold' in vals:
            changed = self.filtered(lambda line: line.sf_on_hold != bool(vals['sf_on_hold']))
        res = super().write(vals)
        if changed:
            changed._sf_sync_deliverable_hold(bool(vals['sf_on_hold']))
        return res

    def _sf_sync_deliverable_hold(self, on_hold):
        """Set the hold state on deliverables that have not been delivered yet.

        Never raises: a failure is logged and the line write goes ahead. The savepoint
        stops a failed deliverable write from aborting the transaction.
        """
        try:
            with self.env.cr.savepoint():
                deliverables = self.env['lb.item.deliverable'].search([
                    ('sale_line_id', 'in', self.ids),
                    ('is_fulfilled', '=', False),
                    ('delivery_marked_datetime', '=', False),
                ])
                deliverables = deliverables.filtered(
                    lambda d: d.dispatch_status != ('on_hold' if on_hold else 'dispatchable')
                )
                deliverables.write({
                    'dispatch_status': 'on_hold' if on_hold else 'dispatchable',
                })
        except Exception:
            _logger.exception(
                'sf_connector: could not set deliverable hold=%s for order lines %s',
                on_hold, self.ids,
            )
