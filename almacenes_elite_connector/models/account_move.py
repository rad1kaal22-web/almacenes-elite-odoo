# -*- coding: utf-8 -*-
import base64
import logging

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.tools import formatLang

from .compat import ea_note

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = 'account.move'

    ea_invoice_number = fields.Char(string="Almacenes Elite invoice", copy=False, readonly=True, index=True)

    @api.model
    def _ea_import_invoice(self, invoice, company):
        """Creates a draft vendor bill (or refund) from an Almacenes Elite invoice, linked to the purchase order lines,
        with the original PDF attached. Never twice: the invoice number is the key."""
        company = company.sudo()
        vendor = company.ea_partner_id
        move_type = 'in_refund' if invoice.get('tipo') == 'abono' else 'in_invoice'
        Move = self.sudo().with_company(company)
        existing = Move.search([('company_id', '=', company.id), ('move_type', '=', move_type),
                                '|', ('ea_invoice_number', '=', invoice.get('numero')),
                                '&', ('partner_id', 'child_of', vendor.commercial_partner_id.id), ('ref', '=', invoice.get('numero'))], limit=1)
        if existing:
            return existing
        PO = self.env['purchase.order'].sudo()
        orders = PO
        for api_ref in invoice.get('refs_cliente') or []:
            orders |= PO.search([('company_id', '=', company.id), ('name', '=', api_ref), ('partner_id', 'child_of', vendor.commercial_partner_id.id)])
        for number in invoice.get('pedidos') or []:
            orders |= PO.search([('company_id', '=', company.id)] + PO._ea_number_domain(number))
        if not orders and not company.ea_bills_all:
            return Move.browse()
        Product = self.env['product.product']
        freight = company.ea_freight_product_id or company._ea_freight_product()
        used = set()
        lines = []
        for api_line in invoice.get('lineas') or []:
            po_line = self.env['purchase.order.line']
            kind = api_line.get('tipo') or 'producto'
            candidates = orders.order_line
            if api_line.get('ref_cliente'):
                candidates = candidates.filtered(lambda l: l.order_id.name == api_line['ref_cliente']) or candidates
            for cand in candidates:
                if cand.id in used or cand.display_type:
                    continue
                if kind == 'producto' and cand.product_id != freight and cand.order_id._ea_line_ref(cand) == api_line.get('ref'):
                    po_line = cand
                    break
                if kind != 'producto' and cand.product_id == freight and abs((cand.price_unit or 0.0) - (api_line.get('precio') or 0.0)) < 0.005:
                    po_line = cand
                    break
            if po_line:
                used.add(po_line.id)
                product = po_line.product_id
            elif kind == 'producto':
                product = Product._ea_find(api_line.get('ref') or '', api_line.get('ean') or '', company)
            else:
                product = freight
            taxes = Product._ea_purchase_taxes(api_line.get('impuestos') or [api_line.get('iva') or 0.0], company)
            if kind == 'producto' and api_line.get('ref'):
                name = '[%s] %s' % (api_line['ref'], api_line.get('nombre') or api_line.get('descripcion') or '')
            else:
                name = api_line.get('nombre') or api_line.get('descripcion') or '/'
            lines.append((0, 0, {
                'product_id': product.id or False, 'name': name, 'quantity': api_line.get('cantidad') or 0.0,
                'price_unit': api_line.get('precio') or 0.0, 'discount': api_line.get('descuento') or 0.0,
                'tax_ids': [(6, 0, taxes.ids)], 'purchase_line_id': po_line.id or False,
            }))
        currency = self.env['res.currency'].search([('name', '=', invoice.get('moneda') or 'EUR')], limit=1)
        move = Move.create({
            'move_type': move_type, 'partner_id': vendor.id, 'company_id': company.id, 'ref': invoice.get('numero'),
            'ea_invoice_number': invoice.get('numero'), 'invoice_date': invoice.get('fecha') or fields.Date.today(),
            'invoice_payment_term_id': False, 'invoice_date_due': invoice.get('vencimiento') or invoice.get('fecha') or fields.Date.today(),
            'invoice_origin': ', '.join(invoice.get('pedidos') or []), 'currency_id': (currency or company.currency_id).id,
            'invoice_line_ids': lines,
        })
        move._ea_align_tax_rounding(invoice)
        pdf = self.env['almacenes.elite.api']._ea_pdf(invoice.get('numero'), company=company)
        if pdf:
            attachment = self.env['ir.attachment'].sudo().create({
                'name': '%s.pdf' % (invoice.get('numero') or 'invoice').replace('/', '_'), 'type': 'binary', 'datas': base64.b64encode(pdf),
                'res_model': 'account.move', 'res_id': move.id, 'mimetype': 'application/pdf'})
            move.message_main_attachment_id = attachment
        difference = abs((move.amount_total or 0.0) - (invoice.get('total') or 0.0))
        if difference > 0.015:
            ea_note(move, Markup('%s') % _("Check this bill: Almacenes Elite invoice total is %(theirs)s and this bill adds up to %(ours)s.",
                                                     theirs=formatLang(self.env, invoice.get('total') or 0.0, currency_obj=move.currency_id),
                                                     ours=formatLang(self.env, move.amount_total, currency_obj=move.currency_id)))
        elif company.ea_post_bills:
            move.action_post()
        ea_note(move, Markup('%s') % _("Created automatically from Almacenes Elite invoice %s.", invoice.get('numero')))
        return move

    def _ea_align_tax_rounding(self, invoice):
        """Almacenes Elite rounds VAT per invoice and Odoo usually per line, so a bill can be a cent off the invoice. Like Odoo
        does with electronic invoices (UBL), the bill keeps the VAT amount of the original invoice when the untaxed amounts
        match and only the rounding differs (at most 5 cents)."""
        self.ensure_one()
        currency = self.currency_id
        if invoice.get('iva') is None or invoice.get('base') is None:
            return False
        if currency.compare_amounts(self.amount_untaxed, float(invoice['base'])) != 0:
            return False
        difference = currency.round(float(invoice['iva']) - self.amount_tax)
        if currency.is_zero(difference) or abs(difference) > 0.05:
            return False
        tax_lines = self.line_ids.filtered('tax_line_id')
        if not tax_lines:
            return False
        with self._get_edi_creation() as move:
            line = tax_lines.sorted(lambda l: -abs(l.amount_currency))[:1]
            sign = -1 if move.is_inbound(include_receipts=True) else 1
            line.amount_currency += sign * difference
        return True
