# -*- coding: utf-8 -*-
import base64
import logging

import requests

from odoo import _, api, fields, models

from .compat import storable_vals

_logger = logging.getLogger(__name__)


class ProductSupplierinfo(models.Model):
    _inherit = 'product.supplierinfo'

    # Almacenes Elite: checked every night (and with «Update prices and stock» on an RFQ)
    ea_stock_state = fields.Selection([('en_stock', "In stock"), ('sin_stock', "Out of stock"), ('sobre_pedido', "On order")],
                                      string="Stock at Almacenes Elite", readonly=True, copy=False)
    ea_stock_text = fields.Char(string="Availability at Almacenes Elite", readonly=True, copy=False)
    ea_expected_date = fields.Date(string="Expected at Almacenes Elite", readonly=True, copy=False)
    ea_alternative = fields.Json(string="Alternative with stock", readonly=True, copy=False)
    ea_checked = fields.Datetime(string="Checked with Almacenes Elite", readonly=True, copy=False)


class ProductProduct(models.Model):
    _inherit = 'product.product'

    @api.model
    def _ea_refresh_products(self, company, products=None):
        """Current price, stock, expected date and alternative with stock of the Almacenes Elite products of this Odoo (only the ones
        bought from them, never the catalog), in batches of 100 references. ``products``: only those. Returns
        {'checked': n, 'prices': n changed, 'unavailable': [codes no longer available]}."""
        company = company.sudo()
        vendor = company.ea_partner_id
        result = {'checked': 0, 'prices': 0, 'unavailable': []}
        if not vendor:
            return result
        Info = self.env['product.supplierinfo'].sudo()
        domain = [('partner_id', '=', vendor.id), ('product_code', '!=', False), ('company_id', 'in', [False, company.id])]
        if products is not None:
            if not products:
                return result
            domain += ['|', ('product_id', 'in', products.ids), ('product_tmpl_id', 'in', products.product_tmpl_id.ids)]
        by_code = {}
        for info in Info.search(domain):
            by_code.setdefault(info.product_code.strip(), Info)
            by_code[info.product_code.strip()] |= info
        codes = sorted(by_code)
        now = fields.Datetime.now()
        Api = self.env['almacenes.elite.api']
        for start in range(0, len(codes), 100):
            chunk = codes[start:start + 100]
            body = Api._ea_get('productos', {'refs': ','.join(chunk), 'disponibilidad': 1}, company=company)
            found = {item.get('ref'): item for item in body.get('productos') or []}
            gone = set(body.get('no_encontrados') or [])
            for code in chunk:
                item = found.get(code)
                if not item:
                    if code in gone:
                        by_code[code].write({'ea_stock_state': 'sin_stock', 'ea_stock_text': _("No longer available at Almacenes Elite"),
                                             'ea_expected_date': False, 'ea_alternative': False, 'ea_checked': now})
                        result['unavailable'].append(code)
                    continue
                stock = item.get('stock') or {}
                vals = {'ea_stock_state': stock.get('estado') if stock.get('estado') in ('en_stock', 'sin_stock', 'sobre_pedido') else False,
                        'ea_stock_text': stock.get('texto') or False, 'ea_expected_date': item.get('prevista') or False,
                        'ea_alternative': item.get('alternativa') or False, 'ea_checked': now}
                for info in by_code[code]:
                    info_vals = dict(vals)
                    if item.get('precio') and abs((info.price or 0.0) - item['precio']) > 0.00005:
                        info_vals['price'] = item['precio']
                        result['prices'] += 1
                    info.write(info_vals)
                result['checked'] += 1
        return result

    @api.model
    def _ea_purchase_taxes(self, percents, company):
        """Purchase taxes of ``company`` matching a list of percentages (e.g. [21.0] or [21.0, 5.2] with surcharge).
        The company default purchase tax wins when it has the same rate."""
        Tax = self.env['account.tax'].sudo()
        default = company.account_purchase_tax_id
        taxes = Tax
        for pct in percents or []:
            pct = round(float(pct or 0.0), 2)
            if not pct:
                # 0 % (e.g. free shipping): no tax rather than the first 0 % tax of the chart (in Spain that is «DUA exento», for imports)
                continue
            if default and default.amount_type == 'percent' and round(default.amount, 2) == pct and default not in taxes:
                taxes |= default
                continue
            found = Tax.search([('type_tax_use', '=', 'purchase'), ('amount_type', '=', 'percent'), ('amount', '=', pct),
                                ('company_id', '=', company.id), ('active', '=', True)], order='sequence, id', limit=1)
            if found:
                taxes |= found
        return taxes

    @api.model
    def _ea_find(self, ref, ean, company):
        """Product already linked to Almacenes Elite (vendor code), or with the same barcode."""
        vendor = company.ea_partner_id
        info = self.env['product.supplierinfo'].sudo().search([
            ('partner_id', '=', vendor.id), ('product_code', '=', ref), ('company_id', 'in', [False, company.id])], limit=1)
        if info:
            return info.product_id or info.product_tmpl_id.product_variant_id
        if ean:
            return self.sudo().search([('barcode', '=', ean), ('company_id', 'in', [False, company.id])], limit=1)
        return self.browse()

    @api.model
    def _ea_get_or_create(self, item, company):
        """Product for a catalog item (dict from the API). Creates it only when it does not exist yet: the Almacenes Elite
        catalog is never imported, only what you buy."""
        company = company.sudo()
        vendor = company.ea_partner_id
        ref, ean = item.get('ref') or '', item.get('ean') or ''
        product = self._ea_find(ref, ean, company)
        seller_vals = {'partner_id': vendor.id, 'product_code': ref, 'product_name': item.get('nombre') or ref,
                       'price': item.get('precio') or 0.0, 'min_qty': item.get('minimo') or 0.0, 'company_id': company.id}
        if product:
            tmpl = product.product_tmpl_id.sudo()
            info = tmpl.seller_ids.filtered(lambda s: s.partner_id == vendor and s.product_code == ref)
            if info:
                info[:1].write({'price': seller_vals['price'], 'min_qty': seller_vals['min_qty']})
            else:
                tmpl.write({'seller_ids': [(0, 0, seller_vals)]})
            return product
        Template = self.env['product.template'].sudo().with_company(company)
        vals = {
            'name': item.get('nombre') or ref, 'type': 'consu', 'sale_ok': False, 'purchase_ok': True,
            'supplier_taxes_id': [(6, 0, self._ea_purchase_taxes([item.get('iva') or 0.0], company).ids)],
            'seller_ids': [(0, 0, seller_vals)], 'company_id': False,
        }
        if ref and not self.sudo().search_count([('default_code', '=', ref)]):
            vals['default_code'] = ref
        if ean and not self.sudo().search_count([('barcode', '=', ean)]):
            vals['barcode'] = ean
        vals.update(storable_vals(Template, company.ea_product_storable))
        if company.ea_fetch_images and item.get('imagen'):
            try:
                resp = requests.get(item['imagen'], timeout=15)
                if resp.status_code == 200 and resp.headers.get('Content-Type', '').startswith('image/'):
                    vals['image_1920'] = base64.b64encode(resp.content)
            except requests.exceptions.RequestException as exc:
                _logger.info("Almacenes Elite image not downloaded for %s: %s", ref, exc)
        return Template.create(vals).product_variant_id
