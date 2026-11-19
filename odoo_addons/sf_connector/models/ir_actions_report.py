# -*- coding: utf-8 -*-
"""Customer invoices and credit notes are rendered with their company's layout whichever report asks for them: the
standard "Invoice PDF" (Print of one or several invoices, Send, the saved PDF) or one of ours. Each invoice is rendered
with its own company's report (paper and template), and the streams come back in the order asked for, so Odoo merges
them into one download. Other documents are left to Odoo.
"""
from collections import OrderedDict, defaultdict

from odoo import models

SF_PDF_TYPES = ('out_invoice', 'out_refund')
SF_TEMPLATE = 'sf_connector.report_sf_invoice'
# Odoo's standard invoice layouts: "Invoice PDF" (with payments) and "PDF without Payment". Never used for customer documents.
STANDARD_INVOICE_TEMPLATES = ('account.report_invoice_with_payments', 'account.report_invoice')


class IrActionsReport(models.Model):
    _inherit = 'ir.actions.report'

    def _pre_render_qweb_pdf(self, report_ref, res_ids=None, data=None):
        report = self._get_report(report_ref)
        moves = self.env['account.move'].browse(res_ids) if res_ids else self.env['account.move']
        if (report.model != 'account.move' or report.report_name not in (SF_TEMPLATE,) + STANDARD_INVOICE_TEMPLATES
                or not moves or any(move.move_type not in SF_PDF_TYPES for move in moves)):
            return super()._pre_render_qweb_pdf(report_ref, res_ids=res_ids, data=data)

        groups = defaultdict(list)  # (our report xmlid, company id) -> invoice ids
        for move in moves:
            groups[(move._sf_invoice_report_xmlid(), move.company_id.id)].append(move.id)

        streams = {}
        for (xmlid, company_id), ids in groups.items():
            action = self.env.ref(xmlid)
            # super() on the company-scoped recordset: this renders with our report, without coming back in here.
            content, report_type = super(IrActionsReport, self.with_company(company_id))._pre_render_qweb_pdf(
                action, res_ids=ids, data=dict(data or {}))
            if report_type != 'pdf':  # test environment: Odoo renders HTML instead
                return super()._pre_render_qweb_pdf(report_ref, res_ids=res_ids, data=data)
            streams.update(content)
        return OrderedDict((move_id, streams[move_id]) for move_id in res_ids if move_id in streams), 'pdf'
