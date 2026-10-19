# -*- coding: utf-8 -*-
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .elite_api import DEFAULT_URL

_logger = logging.getLogger(__name__)


class ResCompany(models.Model):
    _inherit = 'res.company'

    ea_consent = fields.Boolean(
        string="Almacenes Elite connection enabled",
        help="When enabled, Odoo exchanges with Almacenes Elite the data needed to buy from them: the orders placed on "
             "almaceneselite.com from this Odoo, their status, shipments and invoices, and the prices and stock of the products "
             "you buy from them.")
    ea_api_url = fields.Char(string="Almacenes Elite address", default=DEFAULT_URL)
    ea_api_key = fields.Char(string="Almacenes Elite connection key", groups='base.group_system', copy=False)
    ea_partner_id = fields.Many2one('res.partner', string="Almacenes Elite vendor", copy=False)
    ea_account_name = fields.Char(string="Customer account at Almacenes Elite", copy=False, readonly=True)
    ea_addresses = fields.Json(string="Delivery addresses at Almacenes Elite", copy=False)
    ea_shipping_rule = fields.Selection([('sector', "Minimum order"), ('margen', "Calculated in the cart"), ('sin', "No minimum")],
                                        string="Shipping costs rule", copy=False, readonly=True)
    ea_shipping_min = fields.Float(string="No shipping costs from (before VAT)", copy=False, readonly=True)
    ea_shipping_amount = fields.Float(string="Shipping costs below the minimum", copy=False, readonly=True)
    ea_refresh_prices = fields.Boolean(string="Update prices and stock every night", default=True,
                                       help="Every night, the prices, stock, expected dates and alternatives of the Almacenes Elite "
                                            "products you buy are updated, and the RFQs not ordered yet take the current prices.")
    ea_replenish_remind = fields.Boolean(string="Remind me to order replenishment", default=True,
                                         help="When your replenishment rules prepare an RFQ for Almacenes Elite, a to-do reminds you "
                                              "to order it on almaceneselite.com (nothing is ordered by itself).")
    ea_replenish_user_id = fields.Many2one('res.users', string="Replenishment reminders for", copy=False,
                                           domain="[('share', '=', False)]")
    ea_payment_mode = fields.Selection(
        [('account', "Your usual payment terms"), ('web', "Card or PayPal on almaceneselite.com")],
        string="Payment at Almacenes Elite", default='account', copy=False, readonly=True,
        help="Set by Almacenes Elite for your account. With card or PayPal, each order is paid on almaceneselite.com and comes "
             "back to Odoo confirmed.")
    ea_payment_request_date = fields.Datetime(string="Change of payment method requested on", copy=False, readonly=True)
    ea_web_addresses = fields.Selection(
        [('add', "Add them to my contacts"), ('manual', "I will add them myself")],
        string="New delivery addresses from the website", copy=False,
        help="When an order bought on almaceneselite.com goes to an address that is not in this Odoo: add it to your contacts "
             "automatically, or leave a button on the purchase order to add it yourself. Empty: the website asks once.")
    ea_import_bills = fields.Boolean(string="Import Almacenes Elite invoices as vendor bills", default=True)
    ea_bills_all = fields.Boolean(
        string="Also invoices of orders placed outside Odoo",
        help="Also create vendor bills for purchases made on almaceneselite.com, by phone or through your sales rep.")
    ea_post_bills = fields.Boolean(string="Post bills automatically when totals match", default=False)
    ea_product_storable = fields.Boolean(string="Track inventory of new products", default=False)
    ea_fetch_images = fields.Boolean(string="Download product images", default=True)
    ea_freight_product_id = fields.Many2one('product.product', string="Shipping costs product", copy=False)
    ea_orders_since = fields.Char(copy=False)
    ea_bills_since = fields.Char(copy=False)
    ea_last_sync = fields.Datetime(string="Last synchronisation", copy=False, readonly=True)
    ea_last_error = fields.Char(string="Last synchronisation error", copy=False, readonly=True)
    # already a customer: the connection is confirmed by email (or by Almacenes Elite) and the key is collected with this token
    ea_signup_token = fields.Char(string="Pending connection token", groups='base.group_system', copy=False)
    ea_signup_sent_to = fields.Char(string="Confirmation email sent to", copy=False, readonly=True)
    ea_signup_date = fields.Datetime(string="Connection requested on", copy=False, readonly=True)
    # someone has just opened almaceneselite.com from this Odoo: until then, the orders placed there are checked every few minutes
    # (an order placed with the tab closed before coming back arrives without waiting for the hourly update)
    ea_web_open_until = fields.Datetime(string="Check the orders placed on the website often until", copy=False, readonly=True)

    def _ea_connect(self):
        """Reads the customer account at Almacenes Elite and prepares Odoo: vendor contact, delivery addresses,
        shipping costs product. Safe to run again (updates instead of duplicating)."""
        self.ensure_one()
        company = self.sudo()
        body = self.env['almacenes.elite.api']._ea_get('cuenta', company=company)
        vendor = company._ea_vendor_from(body.get('proveedor') or {})
        vals = {
            'ea_partner_id': vendor.id,
            'ea_account_name': (body.get('cliente') or {}).get('nombre') or '',
            'ea_addresses': body.get('direcciones') or [],
            'ea_payment_mode': company._ea_payment_mode_from(body),
            'ea_last_error': False,
        }
        vals.update(company._ea_shipping_from(body))
        if not company.ea_replenish_user_id and not self.env.user.share and self.env.user.active:
            vals['ea_replenish_user_id'] = self.env.user.id
        company.write(vals)
        if not company.ea_freight_product_id:
            company.ea_freight_product_id = company._ea_freight_product()
        return body

    @staticmethod
    def _ea_shipping_from(body):
        """The shipping costs rule of the account: a minimum order (before VAT) below which Almacenes Elite charges shipping, a rule
        calculated in the website cart, or no minimum."""
        rule = (body or {}).get('portes') or {}
        if rule.get('regla') not in ('sector', 'margen', 'sin'):
            return {}
        return {'ea_shipping_rule': rule['regla'], 'ea_shipping_min': float(rule.get('minimo') or 0.0),
                'ea_shipping_amount': float(rule.get('importe') or 0.0)}

    @staticmethod
    def _ea_payment_mode_from(body):
        """«web» when Almacenes Elite says this account pays by card or PayPal (older answers without it: usual terms)."""
        return 'web' if ((body or {}).get('cliente') or {}).get('pago') == 'web' else 'account'

    def _ea_refresh_account(self):
        """Light refresh on every synchronisation: payment method, delivery addresses and shipping rule can change at Almacenes Elite."""
        self.ensure_one()
        company = self.sudo()
        body = self.env['almacenes.elite.api']._ea_get('cuenta', company=company)
        addresses = body.get('direcciones') or []
        vals = {'ea_payment_mode': company._ea_payment_mode_from(body)}
        vals.update(company._ea_shipping_from(body))
        if addresses:
            vals['ea_addresses'] = addresses
        company.write(vals)
        return body

    def _ea_request_payment_change(self):
        """Asks Almacenes Elite to study another payment method (transfer, direct debit, payment terms…). They contact you;
        nothing changes until they do. Returns the notification to show."""
        self.ensure_one()
        company = self.sudo()
        body = self.env['almacenes.elite.api']._ea_get('solicitud_pago', {
            'usuario': self.env.user.name or '', 'email': self.env.user.email or '', 'telefono': self.env.user.partner_id.phone or ''},
            company=company)
        company.ea_payment_request_date = fields.Datetime.now()
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'type': 'success', 'sticky': True, 'title': _("Request sent to Almacenes Elite"),
                           'message': body.get('mensaje') or _("They will study it and contact you."),
                           'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'}}}

    # ------------------------------------------------------------------ already a customer: connect after the email confirmation
    def _ea_wait_for_confirmation(self, body):
        """Almacenes Elite recognised the tax ID of an existing customer: it emails an address it already has for that customer
        to confirm it is them (or checks it itself). Keeps the one-time token to collect the key. Returns True when this Odoo
        is now waiting for the confirmation."""
        self.ensure_one()
        company = self.sudo()
        if body.get('vale'):
            company.write({'ea_signup_token': body['vale'], 'ea_signup_date': fields.Datetime.now(),
                           'ea_signup_sent_to': body.get('enviado_a') or False, 'ea_last_error': False})
            return True
        if company.ea_signup_token:   # asked again a moment later: the first email (and token) still count
            company.ea_signup_sent_to = body.get('enviado_a') or company.ea_signup_sent_to
            return True
        return False

    def _ea_check_signup(self):
        """Has the existing customer confirmed the connection (or has Almacenes Elite approved it)? When so, saves the key and
        connects. Returns 'connected', 'waiting' or 'closed' (rejected or expired: the reason is left in the last error)."""
        self.ensure_one()
        company = self.sudo()
        if not company.ea_signup_token:
            company.write({'ea_signup_date': False, 'ea_signup_sent_to': False})
            return 'closed'
        Api = self.env['almacenes.elite.api']
        status, body = Api._ea_signup_status(company.ea_signup_token, company=company)
        estado = (body or {}).get('estado')
        if status == 200 and estado == 'lista' and body.get('clave'):
            company.write({'ea_api_key': body['clave'], 'ea_consent': True, 'ea_signup_token': False, 'ea_signup_date': False,
                           'ea_signup_sent_to': False, 'ea_last_error': False})
            try:
                company._ea_connect()
            except UserError as exc:   # the key is saved: «Connect and check» finishes it when Almacenes Elite answers again
                company.ea_last_error = str(exc.args[0] if exc.args else exc)[:500]
            return 'connected'
        if status == 200 and estado in ('pendiente', 'revision'):
            if body.get('enviado_a') != company.ea_signup_sent_to:
                company.ea_signup_sent_to = body.get('enviado_a') or False
            return 'waiting'
        if status in (404, 410):
            company.write({'ea_signup_token': False, 'ea_signup_date': False, 'ea_signup_sent_to': False,
                           'ea_last_error': Api._ea_error_text(body)[:500]})
            return 'closed'
        return 'waiting'   # a temporary problem: the next check tries again

    def _ea_cancel_signup(self):
        self.ensure_one()
        self.sudo().write({'ea_signup_token': False, 'ea_signup_date': False, 'ea_signup_sent_to': False})

    @api.model
    def _cron_ea_signup_check(self):
        """Every few minutes, and only when there is something to check (otherwise it does nothing): connections waiting for their
        email confirmation, and the orders placed on almaceneselite.com just after opening it from this Odoo."""
        for company in self.sudo().search([('ea_signup_date', '!=', False)]):
            try:
                company._ea_check_signup()
                self.env.cr.commit()
            except Exception as exc:   # no internet for a moment: try again next time
                self.env.cr.rollback()
                _logger.warning("Almacenes Elite: could not check the pending connection of %s: %s", company.name, exc)
        PurchaseOrder = self.env['purchase.order']
        for company in PurchaseOrder._ea_quick_sync_companies():
            try:
                PurchaseOrder.with_company(company)._ea_sync_company(company, quick=True)
                self.env.cr.commit()
            except Exception as exc:   # the hourly update tries again anyway
                self.env.cr.rollback()
                _logger.warning("Almacenes Elite: could not check the website orders of %s: %s", company.name, exc)

    def _ea_vendor_from(self, prov):
        self.ensure_one()
        Partner = self.env['res.partner'].sudo().with_company(self)
        vat = (prov.get('nif') or '').replace(' ', '').upper()
        vendor = self.ea_partner_id
        if not vendor and vat:
            vendor = Partner.search([('vat', '=', vat), ('parent_id', '=', False), ('company_id', 'in', [False, self.id])], limit=1)
        country = self.env['res.country'].search([('code', '=', prov.get('pais') or 'ES')], limit=1)
        state = self.env['res.country.state']
        if country and prov.get('provincia'):
            name = prov['provincia']
            state = self.env['res.country.state'].search([('country_id', '=', country.id), ('name', '=ilike', name)], limit=1)
            if not state and '(' in name:
                inner = name.split('(', 1)[1].rstrip(')').strip()
                state = self.env['res.country.state'].search([('country_id', '=', country.id), '|', ('name', '=ilike', inner), ('name', '=ilike', name.split('(', 1)[0].strip())], limit=1)
        vals = {
            'name': prov.get('nombre') or 'Almacenes Elite S.L.', 'is_company': True, 'vat': vat or False,
            'street': prov.get('calle') or False, 'street2': prov.get('calle2') or False, 'zip': prov.get('cp') or False,
            'city': prov.get('ciudad') or False, 'country_id': country.id or False, 'state_id': state.id or False,
            'email': prov.get('email') or False, 'phone': prov.get('telefono') or False, 'website': prov.get('web') or False,
        }
        if vendor:
            vendor.write({k: v for k, v in vals.items() if v and k not in ('name', 'vat')})
        else:
            vals['supplier_rank'] = 1
            vendor = Partner.create(vals)
        return vendor

    def _ea_freight_product(self):
        self.ensure_one()
        Product = self.env['product.product'].sudo().with_context(lang=self.partner_id.lang or self.env.user.lang)
        product = Product.search([('default_code', '=', 'EA-PORTES'), ('company_id', 'in', [False, self.id])], limit=1)
        if not product:
            product = Product.create({
                'name': _("Shipping costs (Almacenes Elite)"), 'default_code': 'EA-PORTES', 'type': 'service',
                'sale_ok': False, 'purchase_ok': True,
                'supplier_taxes_id': [(6, 0, self.env['product.product']._ea_purchase_taxes([21.0], self).ids)],
            })
        return product
