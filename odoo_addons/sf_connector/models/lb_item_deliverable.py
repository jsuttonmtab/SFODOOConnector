# -*- coding: utf-8 -*-
from odoo import api, models


class LbItemDeliverable(models.Model):
    _inherit = 'lb.item.deliverable'

    @api.model_create_multi
    def create(self, vals_list):
        # A deliverable created for a line that is already on hold starts on hold.
        line_ids = [vals['sale_line_id'] for vals in vals_list if vals.get('sale_line_id')]
        on_hold_line_ids = set(
            self.env['sale.order.line'].browse(line_ids).filtered('sf_on_hold').ids
        )
        for vals in vals_list:
            if vals.get('sale_line_id') in on_hold_line_ids:
                vals['dispatch_status'] = 'on_hold'
        return super().create(vals_list)
