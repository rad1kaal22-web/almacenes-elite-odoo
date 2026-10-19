# -*- coding: utf-8 -*-
"""Purchase orders and Almacenes Elite.

Orders are always placed on almaceneselite.com (decided by Almacenes Elite, so that there is a single way to order): this Odoo
opens the website already signed in, the order is placed there (with the website's prices, approvals and payment) and it comes
back here as a confirmed purchase order. An RFQ of this Odoo (for instance one created by the replenishment rules) can be taken
to the website cart with one click; the order placed there confirms that same RFQ. Status, shipments, tracking numbers and
invoices come back by themselves."""
import json
import logging
from datetime import timedelta

from markupsafe import Markup, escape

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_is_zero, format_date, formatLang

from .compat import ea_note, po_line_taxes_field, po_line_uom

_logger = logging.getLogger(__name__)

EA_STATES = [
    ('not_sent', 'Not ordered yet'),
    ('payment', 'Open on almaceneselite.com'),
    ('confirmed', 'Confirmed'),
    ('approval', 'Waiting for approval in your company'),
    ('shipped', 'Shipped'),
    ('rejected', 'Rejected'),
    ('cancelled', 'Cancelled'),
]
API_STATE = {'confirmado': 'confirmed', 'pendiente_aprobacion': 'approval', 'presupuesto': 'approval', 'pendiente_pago': 'payment',
             'rechazado': 'rejected', 'cancelado': 'cancelled'}
OPEN_STATES = ('draft', 'sent', 'to approve', 'purchase')   # an order of this Odoo that can still be placed on the website
WEB_OPEN_HOURS = 12   # after opening the website from Odoo, its orders are checked every few minutes for this long


class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    ea_is_elite = fields.Boolean(compute='_compute_ea_is_elite', string="Ordered from Almacenes Elite")
    ea_state = fields.Selection(EA_STATES, string="Almacenes Elite status", default='not_sent', copy=False, tracking=True)
    ea_order_numbers = fields.Char(string="Almacenes Elite order", copy=False, readonly=True)
    ea_address_choice = fields.Selection(selection='_ea_address_selection', string="Delivered to", copy=False, readonly=True,
                                         help="Delivery address of the order at Almacenes Elite.")
    ea_shipments = fields.Json(copy=False)
    ea_tracking = fields.Html(string="Shipments", compute='_compute_ea_tracking', sanitize=True)
    ea_message = fields.Text(string="Notes from Almacenes Elite", copy=False, readonly=True)
    ea_last_sync = fields.Datetime(string="Last update from Almacenes Elite", copy=False, readonly=True)
    ea_shipments_seen = fields.Char(copy=False)
    ea_from_web = fields.Boolean(string="Bought on almaceneselite.com", copy=False, readonly=True)
    ea_payment_mode = fields.Selection(related='company_id.ea_payment_mode')
    ea_new_address = fields.Json(string="Delivery address to add", copy=False)
    ea_new_address_text = fields.Char(string="Delivery address not in your contacts", compute='_compute_ea_new_address_text')
    ea_reminded = fields.Boolean(string="Replenishment reminder sent", copy=False)
    ea_shipping_hint = fields.Char(string="Shipping costs at Almacenes Elite", compute='_compute_ea_hints')
    ea_availability = fields.Html(string="Availability at Almacenes Elite", compute='_compute_ea_hints', sanitize=True)
    ea_has_alternatives = fields.Boolean(string="Has alternatives with stock", compute='_compute_ea_hints')

    @api.depends('partner_id', 'company_id.ea_partner_id')
    def _compute_ea_is_elite(self):
        for order in self:
            vendor = order.company_id.ea_partner_id
            order.ea_is_elite = bool(vendor) and order.partner_id.commercial_partner_id == vendor.commercial_partner_id

    @api.depends('ea_shipments')
    def _compute_ea_tracking(self):
        labels = {'preparando': _("Preparing"), 'enviado': _("Shipped"), 'anulado': _("Cancelled")}
        for order in self:
            data = order.ea_shipments or {}
            rows = []
            for ship in data.get('entregas') or []:
                link, number = ship.get('url_seguimiento'), ship.get('seguimiento') or ''
                tracking = Markup('<a href="%s" target="_blank" rel="noopener">%s</a>') % (link, number) if link and number else escape(number)
                rows.append(Markup('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>') % (
                    ship.get('albaran') or '', labels.get(ship.get('estado'), ship.get('estado') or ''),
                    (ship.get('fecha_envio') or ship.get('fecha_prevista') or '')[:10], ship.get('transportista') or '', tracking))
            html = Markup('')
            if rows:
                html = Markup('<table class="table table-sm o_ea_shipments"><thead><tr><th>%s</th><th>%s</th><th>%s</th><th>%s</th><th>%s</th></tr></thead><tbody>%s</tbody></table>') % (
                    _("Delivery"), _("Status"), _("Date"), _("Carrier"), _("Tracking"), Markup('').join(rows))
            pending = data.get('pendiente') or []
            if pending:
                items = []
                for p in pending:
                    text = '%s × %g' % (p.get('ref'), p.get('cantidad') or 0)
                    if p.get('prevista'):
                        text += ' (%s)' % _("expected %s", order._ea_date(p['prevista']))
                    items.append(text)
                html += Markup('<p>%s</p>') % _("Still to be delivered: %s", ', '.join(items))
            order.ea_tracking = html or False

    @api.depends('ea_new_address')
    def _compute_ea_new_address_text(self):
        for order in self:
            order.ea_new_address_text = self._ea_address_text(order.ea_new_address) if order.ea_new_address else False

    @api.depends('order_line.product_id', 'order_line.price_subtotal', 'state', 'ea_state', 'company_id.ea_shipping_rule',
                 'company_id.ea_shipping_min')
    def _compute_ea_hints(self):
        """For an RFQ that has not been ordered yet: how much is missing to avoid shipping costs, and the products that
        Almacenes Elite has no stock of (expected date and the alternative with stock), from the last check of prices and stock."""
        for order in self:
            order.ea_shipping_hint = False
            order.ea_availability = False
            order.ea_has_alternatives = False
            if not order.ea_is_elite or order.ea_state not in ('not_sent', 'rejected') or order.state not in OPEN_STATES:
                continue
            company = order.company_id
            freight = company.ea_freight_product_id
            lines = order.order_line.filtered(lambda l: not l.display_type and l.product_id and l.product_id != freight)
            if company.ea_shipping_rule == 'sector' and company.ea_shipping_min and lines:
                base = sum(lines.mapped('price_subtotal'))
                money = lambda value: formatLang(self.env, value, currency_obj=order.currency_id)
                missing = company.ea_shipping_min - base
                if missing > 0.005:
                    order.ea_shipping_hint = _("%(missing)s more (before VAT) and there are no shipping costs: below %(minimum)s Almacenes Elite "
                                               "charges %(amount)s.", missing=money(missing), minimum=money(company.ea_shipping_min),
                                               amount=money(company.ea_shipping_amount or 0.0))
                else:
                    order.ea_shipping_hint = _("No shipping costs: this order reaches the %s minimum.", money(company.ea_shipping_min))
            rows = []
            for line in lines:
                info = order._ea_seller(line.product_id)
                if not info or info.ea_stock_state not in ('sin_stock', 'sobre_pedido'):
                    continue
                name = '[%s] %s' % (info.product_code, info.product_name or line.product_id.name)
                if info.ea_stock_state == 'sobre_pedido':
                    rows.append(Markup('<li>%s: %s</li>') % (name, info.ea_stock_text or _("on order")))
                    continue
                parts = [_("no stock right now")]
                if info.ea_expected_date:
                    parts.append(_("expected %s", format_date(self.env, info.ea_expected_date)))
                alt = info.ea_alternative or {}
                if alt.get('ref'):
                    parts.append(_("alternative with stock: [%(ref)s] %(name)s (%(price)s)", ref=alt['ref'], name=alt.get('nombre') or '',
                                   price=formatLang(self.env, alt.get('precio') or 0.0, currency_obj=order.currency_id)))
                    order.ea_has_alternatives = True
                rows.append(Markup('<li>%s: %s</li>') % (name, ' · '.join(parts)))
            if rows:
                order.ea_availability = Markup('<ul class="mb-0">%s</ul>') % Markup('').join(rows)

    @api.model
    def _ea_address_selection(self):
        out = []
        for addr in self.env.company.ea_addresses or []:
            label = ', '.join(x for x in (addr.get('nombre'), addr.get('calle'), addr.get('ciudad')) if x)
            out.append((str(addr.get('id')), label))
        return out

    def _ea_date(self, value):
        try:
            return format_date(self.env, fields.Date.to_date(value))
        except (TypeError, ValueError):
            return value

    # ------------------------------------------------------------------ products of the order
    def _ea_seller(self, product):
        """The Almacenes Elite vendor line (code, price, stock checked at night) of a product."""
        vendor = self.company_id.ea_partner_id
        if not vendor or not product:
            return self.env['product.supplierinfo']
        infos = product.seller_ids.filtered(lambda s: s.partner_id == vendor and s.product_code
                                           and (not s.product_id or s.product_id == product))
        return infos[:1]

    def _ea_line_ref(self, line):
        info = self._ea_seller(line.product_id)
        return info.product_code or line.product_id.default_code or ''

    def _ea_cart_lines(self):
        """The lines of this RFQ for the website cart (Almacenes Elite codes and whole quantities)."""
        self.ensure_one()
        freight = self.company_id.ea_freight_product_id
        lines, missing, not_integer = [], [], []
        for line in self.order_line:
            if line.display_type or not line.product_id or (freight and line.product_id == freight):
                continue
            qty = po_line_uom(line)._compute_quantity(line.product_qty, line.product_id.uom_id)
            if float_is_zero(qty, precision_digits=4):
                continue
            ref = self._ea_line_ref(line)
            if not ref:
                missing.append(line.product_id.display_name)
                continue
            if abs(qty - round(qty)) > 0.0001:
                not_integer.append(line.product_id.display_name)
                continue
            lines.append({'ref': ref, 'cantidad': int(round(qty))})
        if missing:
            raise UserError(_("These products have no Almacenes Elite code. Buy them once on almaceneselite.com (Purchase › Almacenes "
                              "Elite › Shop on almaceneselite.com) or set their vendor product code:\n%s", '\n'.join('• ' + m for m in missing)))
        if not_integer:
            raise UserError(_("Almacenes Elite sells whole units. Fix the quantity of:\n%s", '\n'.join('• ' + m for m in not_integer)))
        if not lines:
            raise UserError(_("There is nothing to order: add products to the RFQ first."))
        return lines

    def _ea_cost_center(self):
        """The analytic account of the order (by amount, over all its lines) travels as the cost center: Almacenes Elite prints it
        next to the order number on its delivery note and invoice, and assigns it to the same cost center of its B2B portal when
        there is one. Several accounts: all of them in the label, the main one first."""
        self.ensure_one()
        weights = {}
        for line in self.order_line:
            if line.display_type or not line.analytic_distribution:
                continue
            for key, percent in line.analytic_distribution.items():
                for account_id in str(key).split(','):
                    if account_id.strip().isdigit():
                        aid = int(account_id)
                        weights[aid] = weights.get(aid, 0.0) + abs(line.price_subtotal or 0.0) * float(percent or 0.0) / 100.0
        accounts = self.env['account.analytic.account'].browse(list(weights)).exists()
        if not accounts:
            return {}
        accounts = accounts.sorted(lambda a: -weights.get(a.id, 0.0))
        main = accounts[0]
        if len(accounts) == 1:
            label = ' — '.join(x for x in (main.code, main.name) if x)
            return {'codigo': main.code or '', 'nombre': main.name or '', 'etiqueta': label}
        total = sum(weights.get(a.id, 0.0) for a in accounts) or 1.0
        label = ' · '.join('%s %d %%' % (a.code or a.name, round(100.0 * weights.get(a.id, 0.0) / total)) for a in accounts[:3])
        return {'etiqueta': label}

    @api.model
    def _ea_problems_text(self, body):
        rows = []
        for err in body.get('errores') or []:
            rows.append('• %s%s' % ((err.get('ref') + ': ') if err.get('ref') else '', err.get('motivo') or ''))
        if not rows:
            rows.append(self.env['almacenes.elite.api']._ea_error_text(body))
        return '\n'.join(rows)

    # ------------------------------------------------------------------ ordering on almaceneselite.com
    def action_ea_order_on_web(self):
        """«Order on almaceneselite.com»: the website opens already signed in with the products of this RFQ in a new cart (same
        prices, rules, approvals and payment as always on the website). The order is placed there and this same RFQ comes back
        confirmed with the Almacenes Elite order number (never a second purchase order)."""
        self.ensure_one()
        if not self.ea_is_elite:
            raise UserError(_("This RFQ is not addressed to Almacenes Elite."))
        if self.ea_state not in ('not_sent', 'rejected', 'payment') or self.state not in OPEN_STATES:
            raise UserError(_("This order was already placed at Almacenes Elite (%s).", self.ea_order_numbers or ''))
        params = self._ea_entry_params(self.company_id, 'pedido')
        params.update({'lineas': json.dumps(self._ea_cart_lines()), 'ref_cliente': self.name})
        cost_center = self._ea_cost_center()
        if cost_center:
            params['centro_coste'] = json.dumps(cost_center)
        status, body = self.env['almacenes.elite.api']._ea_post('entrar', params, company=self.company_id)
        if status != 200 or not body.get('ok') or not body.get('url'):
            raise UserError(_("Almacenes Elite cannot open this order on the website:\n%s", self._ea_problems_text(body)))
        self._ea_web_opened(self.company_id)
        if self.ea_state != 'payment':
            self.write({'ea_state': 'payment', 'ea_message': False})
            ea_note(self, _("Opened on almaceneselite.com with these products in the cart. When the order is placed there, this RFQ "
                            "is confirmed here with the Almacenes Elite order number."))
        return {'type': 'ir.actions.act_url', 'url': body['url'], 'target': 'self'}

    @api.model
    def action_ea_punchout(self):
        """«Shop on almaceneselite.com»: the website opens already signed in as the company's web buyer. When the order is placed,
        the website comes back to this Odoo and the purchase order is created."""
        company = self.env.company
        if not company.ea_partner_id:
            raise UserError(_("Connect Almacenes Elite first (Purchase › Configuration › Settings)."))
        body = self.env['almacenes.elite.api']._ea_get('entrar', self._ea_entry_params(company, 'pedido'), company=company)
        if not body.get('url'):
            raise UserError(_("Almacenes Elite did not send the link to the website. Try again in a moment."))
        self._ea_web_opened(company)
        return {'type': 'ir.actions.act_url', 'url': body['url'], 'target': 'self'}

    @api.model
    def action_ea_portal(self):
        """The B2B portal of almaceneselite.com (approvals, cost centers, budgets, the company's consumption, reports), already
        signed in. Anything bought there comes back to this Odoo like any purchase on the website."""
        company = self.env.company
        if not company.ea_partner_id:
            raise UserError(_("Connect Almacenes Elite first (Purchase › Configuration › Settings)."))
        params = self._ea_entry_params(company, 'pedido')
        params['destino'] = 'portal'
        body = self.env['almacenes.elite.api']._ea_get('entrar', params, company=company)
        if not body.get('url'):
            raise UserError(_("Almacenes Elite did not send the link to the website. Try again in a moment."))
        self._ea_web_opened(company)
        return {'type': 'ir.actions.act_url', 'url': body['url'], 'target': 'self'}

    def action_ea_return(self):
        """«Return or claim»: almaceneselite.com opens already signed in on «My returns» with this order chosen (returns and claims
        are handled there, with their conditions and status)."""
        self.ensure_one()
        number = (self.ea_order_numbers or '').split(',')[0].strip()
        if not self.ea_is_elite or not number:
            raise UserError(_("This purchase order has no Almacenes Elite order yet."))
        params = self._ea_entry_params(self.company_id, 'pedido')
        params.update({'destino': 'devolucion', 'pedido': number})
        body = self.env['almacenes.elite.api']._ea_get('entrar', params, company=self.company_id)
        if not body.get('url'):
            raise UserError(_("Almacenes Elite did not send the link to the website. Try again in a moment."))
        return {'type': 'ir.actions.act_url', 'url': body['url'], 'target': 'self'}

    @api.model
    def _ea_web_opened(self, company):
        """Someone opened almaceneselite.com from this Odoo: for the next hours the orders placed there are checked every few
        minutes, so that an order placed with the tab closed before coming back arrives here without waiting for the hourly update."""
        company.sudo().write({'ea_web_open_until': fields.Datetime.now() + timedelta(hours=WEB_OPEN_HOURS)})

    @api.model
    def _ea_quick_sync_companies(self):
        return self.env['res.company'].sudo().search([('ea_web_open_until', '>', fields.Datetime.now()), ('ea_consent', '=', True),
                                                      ('ea_api_key', '!=', False), ('ea_partner_id', '!=', False)])

    @api.model
    def _ea_entry_params(self, company, mode):
        """What the website needs to bring the person back: this Odoo's return address, the mode, whether new delivery
        addresses may be added here and which of the account's addresses this Odoo already has (to warn about the others)."""
        base = (self.env['ir.config_parameter'].sudo().get_param('web.base.url') or '').rstrip('/')
        return {'volver': base + '/almacenes_elite/volver', 'modo': mode,
                'dir_odoo': {'add': 'si', 'manual': 'no'}.get(company.ea_web_addresses or '', ''),
                'dir_conocidas': ','.join(str(i) for i in self._ea_known_address_ids(company))}

    def action_ea_request_payment_change(self):
        return (self[:1].company_id or self.env.company)._ea_request_payment_change()

    # ------------------------------------------------------------------ prices, stock and alternatives (checked every night)
    def action_ea_refresh(self):
        """«Update prices and stock»: now, instead of waiting for the night."""
        self.ensure_one()
        products = self.order_line.mapped('product_id')
        result = self.env['product.product']._ea_refresh_products(self.company_id, products)
        changed = self._ea_update_rfq_prices()
        message = _("Prices and stock checked with Almacenes Elite.")
        if changed:
            message += ' ' + _("New prices on %s lines.", changed)
        if result.get('unavailable'):
            message += ' ' + _("No longer available: %s.", ', '.join(result['unavailable']))
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'type': 'info', 'sticky': False, 'title': _("Almacenes Elite"), 'message': message,
                           'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'}}}

    def _ea_update_rfq_prices(self):
        """RFQs not ordered yet take the current Almacenes Elite price of each product. Returns how many lines changed."""
        changed = 0
        for order in self.filtered(lambda o: o.ea_is_elite and o.ea_state in ('not_sent', 'rejected') and o.state in ('draft', 'sent')):
            rows = []
            for line in order.order_line:
                info = order._ea_seller(line.product_id)
                if line.display_type or not info or not info.price:
                    continue
                price = line.product_id.uom_id._compute_price(info.price, po_line_uom(line))
                if abs((line.price_unit or 0.0) - price) > 0.00005:
                    rows.append('[%s] %s → %s' % (info.product_code, formatLang(self.env, line.price_unit, currency_obj=order.currency_id),
                                                  formatLang(self.env, price, currency_obj=order.currency_id)))
                    line.price_unit = price
            if rows:
                changed += len(rows)
                ea_note(order, Markup('%s<br/>%s') % (_("Prices updated with the current Almacenes Elite prices:"), Markup('<br/>').join(rows)))
        return changed

    def action_ea_use_alternatives(self):
        """The products without stock at Almacenes Elite are replaced by the alternative with stock that the website suggests
        (same quantity; the product is created in this Odoo only if it is not there yet)."""
        self.ensure_one()
        Product = self.env['product.product']
        company = self.company_id.sudo()
        rows = []
        for line in self.order_line:
            info = self._ea_seller(line.product_id)
            alt = (info.ea_alternative or {}) if info and info.ea_stock_state == 'sin_stock' else {}
            if line.display_type or not alt.get('ref'):
                continue
            new_product = Product._ea_get_or_create(dict(alt, minimo=0), company)
            old_name = line.product_id.display_name
            qty = line.product_qty
            taxes = Product._ea_purchase_taxes([alt.get('iva') or 0.0], company) if alt.get('iva') else new_product.supplier_taxes_id.filtered(
                lambda t: t.company_id in (company, company.parent_id) or not t.company_id)
            line.write({'product_id': new_product.id, 'product_qty': qty, 'name': new_product.display_name,
                        'price_unit': alt.get('precio') or 0.0, po_line_taxes_field(self.env): [(6, 0, taxes.ids)]})
            rows.append('%s → %s' % (old_name, new_product.display_name))
        if not rows:
            raise UserError(_("There are no alternatives to use: check prices and stock again."))
        ea_note(self, Markup('%s<br/>%s') % (_("Products without stock replaced by the alternative with stock:"), Markup('<br/>').join(rows)))
        return True

    # ------------------------------------------------------------------ replenishment
    def _ea_is_replenishment(self):
        """Created by the replenishment rules (reordering rules) of this Odoo."""
        self.ensure_one()
        lines = self.order_line.filtered(lambda l: not l.display_type)
        if 'orderpoint_id' in lines._fields and any(lines.mapped('orderpoint_id')):
            return True
        return 'OP/' in (self.origin or '')

    @api.model
    def _ea_remind_replenishment(self, company):
        """Once per RFQ: a to-do to order on almaceneselite.com what the replenishment rules have put in an RFQ for Almacenes Elite
        (nothing is ordered by itself: the order is placed on the website)."""
        company = company.sudo()
        vendor = company.ea_partner_id
        if not vendor or not company.ea_replenish_remind:
            return 0
        orders = self.sudo().search([('company_id', '=', company.id), ('partner_id', 'child_of', vendor.commercial_partner_id.id),
                                     ('state', 'in', ('draft', 'sent')), ('ea_state', '=', 'not_sent'), ('ea_reminded', '=', False)])
        count = 0
        for order in orders.filtered(lambda o: o._ea_is_replenishment() and o.order_line):
            user = company.ea_replenish_user_id
            if not user and order.user_id.active and not order.user_id.share:
                user = order.user_id
            user = user or self.env.ref('base.user_admin', raise_if_not_found=False) or self.env.user
            order.with_context(lang=user.lang or company.partner_id.lang or self.env.user.lang)._ea_schedule_replenishment(user)
            order.ea_reminded = True
            count += 1
        return count

    def _ea_schedule_replenishment(self, user):
        # (in a method of the order, so the texts are in the language of the person who gets the to-do)
        self.ensure_one()
        self.activity_schedule('mail.mail_activity_data_todo', summary=_("Replenishment: order it on almaceneselite.com"),
                               note=_("Your replenishment rules have prepared this RFQ for Almacenes Elite. Press «Order on "
                                      "almaceneselite.com»: the website opens with these products in the cart and the order is placed there."),
                               user_id=user.id)

    # ------------------------------------------------------------------ back from almaceneselite.com
    @api.model
    def _ea_web_lines(self, api_lines, company, with_extra=True):
        """Purchase order lines for lines of an Almacenes Elite order (products created only when needed)."""
        Product = self.env['product.product']
        freight = company.ea_freight_product_id or company._ea_freight_product()
        taxes_field = po_line_taxes_field(self.env)
        lines = []
        for api_line in api_lines or []:
            kind = api_line.get('tipo') or 'producto'
            if kind != 'producto' and not with_extra:
                continue
            if kind == 'producto':
                item = dict(api_line, nombre=api_line.get('nombre') or api_line.get('descripcion') or api_line.get('ref'), minimo=0)
                vals = {'product_id': Product._ea_get_or_create(item, company).id}
            else:
                vals = {'product_id': freight.id, 'name': api_line.get('nombre') or api_line.get('descripcion') or freight.name}
            vals.update({'product_qty': api_line.get('cantidad') or (0.0 if kind == 'producto' else 1.0),
                         'price_unit': api_line.get('precio') or 0.0,
                         taxes_field: [(6, 0, Product._ea_purchase_taxes(api_line.get('impuestos') or [api_line.get('iva') or 0.0], company).ids)]})
            if api_line.get('descuento'):
                vals['discount'] = api_line['descuento']
            lines.append((0, 0, vals))
        return lines

    def _ea_apply_order_result(self, body, paid_on_web=False):
        """Mirrors the order placed at Almacenes Elite: quantities they adjusted, their prices, shipping and other lines."""
        self.ensure_one()
        orders = body.get('pedidos') or []
        freight = self.company_id.ea_freight_product_id or self.company_id.sudo()._ea_freight_product()
        by_ref = {}
        extra = []
        for order in orders:
            for api_line in order.get('lineas') or []:
                if api_line.get('tipo') == 'producto':
                    by_ref.setdefault(api_line.get('ref'), []).append(api_line)
                elif api_line.get('tipo') in ('portes', 'descuento', 'servicio'):
                    extra.append(api_line)
        for line in self.order_line:
            if line.display_type or not line.product_id or line.product_id == freight:
                continue
            matches = by_ref.get(self._ea_line_ref(line))
            if not matches:
                continue
            api_line = matches.pop(0)
            qty = line.product_id.uom_id._compute_quantity(api_line.get('cantidad') or 0.0, po_line_uom(line))
            vals = {}
            if abs(qty - line.product_qty) > 0.0001:
                vals['product_qty'] = qty
            vals['price_unit'] = line.product_id.uom_id._compute_price(api_line.get('precio') or 0.0, po_line_uom(line))
            if api_line.get('descuento'):
                vals['discount'] = api_line['descuento']
            line.write(vals)
        # products added on the website (not in the RFQ) become lines too, so the order is the same as at Almacenes Elite
        leftovers = [x for group in by_ref.values() for x in group]
        new_lines = self._ea_web_lines(leftovers, self.company_id.sudo(), with_extra=False) if leftovers else []
        # one line per shipping/discount line of Almacenes Elite, so the order total matches theirs
        self.order_line.filtered(lambda l: l.product_id == freight).unlink()
        for api_line in extra:
            new_lines.append((0, 0, {
                'product_id': freight.id, 'name': api_line.get('nombre') or api_line.get('descripcion') or freight.name,
                'product_qty': api_line.get('cantidad') or 1.0, 'price_unit': api_line.get('precio') or 0.0,
                po_line_taxes_field(self.env): [(6, 0, self.env['product.product']._ea_purchase_taxes(api_line.get('impuestos') or [api_line.get('iva') or 0.0], self.company_id).ids)],
            }))
        numbers = ', '.join(o.get('numero') for o in orders if o.get('numero'))
        state = API_STATE.get(body.get('estado'), 'confirmed')
        self.write({'order_line': new_lines, 'partner_ref': numbers or self.partner_ref, 'ea_order_numbers': numbers,
                    'ea_state': state, 'ea_message': False, 'ea_last_sync': fields.Datetime.now()})
        if paid_on_web:
            rows = [escape(_("Ordered on almaceneselite.com: Almacenes Elite order %s.", numbers or '-'))]
        else:
            rows = [escape(_("Placed at Almacenes Elite: order %s.", numbers or '-'))]
        if body.get('repetido'):
            rows.append(escape(_("It had already been received: the existing order is kept (no duplicate).")))
        for adj in body.get('ajustes') or []:
            rows.append(escape(_("%(ref)s: quantity %(old)s changed to %(new)s (%(why)s)", ref=adj.get('ref'), old=adj.get('pedida'),
                                 new=adj.get('cantidad'), why=adj.get('motivo'))))
        for why in body.get('aprobacion') or []:
            rows.append(escape(_("Waiting for approval in your company: %s", why)))
        total = sum(o.get('total') or 0.0 for o in orders)
        if abs(total - self.amount_total) > 0.015:
            rows.append(escape(_("Check the amounts: Almacenes Elite total %(theirs)s, this order %(ours)s.",
                                 theirs=formatLang(self.env, total, currency_obj=self.currency_id),
                                 ours=formatLang(self.env, self.amount_total, currency_obj=self.currency_id))))
        ea_note(self, Markup('<br/>').join(rows))

    def _approval_allowed(self):
        # an order already placed at Almacenes Elite (on the website, with the website's own approvals) is confirmed here as it is:
        # this Odoo does not ask for its approval again
        return bool(self.env.context.get('ea_decided')) or super()._approval_allowed()

    def _ea_confirm_decided(self):
        """Confirms purchase orders that are already placed at Almacenes Elite (on the website)."""
        for order in self:
            decided = order.with_context(ea_decided=True)
            if order.state in ('draft', 'sent'):
                decided.button_confirm()
            if order.state == 'to approve':
                decided.button_approve()

    # ------------------------------------------------------------------ delivery addresses of web orders
    @api.model
    def _ea_address_text(self, addr):
        addr = addr or {}
        return ', '.join(x for x in (addr.get('nombre'), addr.get('calle'), addr.get('calle2'),
                                     ' '.join(y for y in (addr.get('cp'), addr.get('ciudad')) if y), addr.get('provincia')) if x)

    @api.model
    def _ea_address_key(self, street, zip_code):
        norm = lambda text: ''.join(ch for ch in (text or '').lower() if ch.isalnum())
        return norm(zip_code), norm(street)

    @api.model
    def _ea_address_partner(self, addr, company, create=False):
        """The contact of this Odoo with the same street and postcode as an Almacenes Elite delivery address (the company or
        one of its contacts). With ``create``, a new delivery contact under the company when there is none."""
        Partner = self.env['res.partner'].sudo()
        key = self._ea_address_key(addr.get('calle'), addr.get('cp'))
        if not key[1]:
            return Partner.browse()
        own = company.partner_id
        for partner in Partner.search([('id', 'child_of', own.id)]):
            if self._ea_address_key(partner.street, partner.zip) == key:
                return partner
        if not create:
            return Partner.browse()
        country = self.env['res.country'].search([('code', '=', addr.get('pais') or 'ES')], limit=1)
        state = self.env['res.country.state'].search([('country_id', '=', country.id), ('name', '=ilike', addr.get('provincia') or '-')], limit=1) if country else False
        return Partner.create({
            'parent_id': own.id, 'type': 'delivery', 'name': addr.get('nombre') or _("Delivery address"),
            'street': addr.get('calle') or False, 'street2': addr.get('calle2') or False, 'zip': addr.get('cp') or False,
            'city': addr.get('ciudad') or False, 'country_id': country.id or False, 'state_id': state.id if state else False,
            'phone': addr.get('telefono') or False,
        })

    @api.model
    def _ea_known_address_ids(self, company):
        """Almacenes Elite delivery addresses (ids) that this Odoo already has as contacts."""
        return [addr['id'] for addr in company.ea_addresses or [] if addr.get('id') and self._ea_address_partner(addr, company)]

    def _ea_web_delivery(self, api_order, permission=None):
        """Delivery address of an order bought on the website. Already in this Odoo: shown on the order. New: added to the
        contacts when the company allows it (or the person said yes on the website); otherwise the order shows it with a
        one-click button and a to-do."""
        self.ensure_one()
        company = self.company_id.sudo()
        addr = api_order.get('direccion_entrega') or {}
        if not addr.get('id'):
            return
        known = company.ea_addresses or []
        if not any(a.get('id') == addr['id'] for a in known):
            company.ea_addresses = known + [addr]
        order = self.with_company(company)
        vals = {'ea_address_choice': str(addr['id'])}
        if not self._ea_address_partner(addr, company):
            if (permission or company.ea_web_addresses) == 'add':
                partner = self._ea_address_partner(addr, company, create=True)
                ea_note(order, _("New delivery address added to your contacts: %s", partner.display_name))
            else:
                vals['ea_new_address'] = addr
                order.activity_schedule('mail.mail_activity_data_todo', summary=_("Add the delivery address to your contacts"),
                                        note=self._ea_address_text(addr), user_id=order.user_id.id or self.env.uid)
        order.write(vals)

    def action_ea_add_address(self):
        """One click: the delivery address of this order becomes a delivery contact of your company."""
        todo = self.env.ref('mail.mail_activity_data_todo', raise_if_not_found=False)
        for order in self.filtered('ea_new_address'):
            addr = order.ea_new_address
            partner = self._ea_address_partner(addr, order.company_id.sudo(), create=True)
            text = self._ea_address_text(addr)
            order.activity_ids.filtered(lambda a: a.activity_type_id == todo and text in (a.note or '')).action_feedback(
                feedback=_("Added to your contacts."))
            order.write({'ea_new_address': False})
            ea_note(order, _("Delivery address added to your contacts: %s", partner.display_name))
        return True

    # ------------------------------------------------------------------ orders placed on the website
    @api.model
    def _ea_import_web_order(self, number, company=None, api_order=None, permission=None):
        """Purchase order for an order placed on almaceneselite.com from this Odoo. Never twice: the order number is the
        key. Confirmed at Almacenes Elite → confirmed here; waiting for approval → RFQ until it is approved."""
        company = (company or self.env.company).sudo()
        if api_order is None:
            api_order = self._ea_fetch_web_order(number, company)
        permission = permission or {'si': 'add', 'no': 'manual'}.get(api_order.get('direccion_a_odoo') or '')
        existing = self._ea_find_orders(api_order, company)
        if existing:
            self._ea_update_from_api(api_order, company)
            return existing[:1]
        state = API_STATE.get(api_order.get('estado'), 'confirmed')
        if state in ('rejected', 'cancelled'):
            return self.browse()
        order = self.with_company(company).create({
            'partner_id': company.ea_partner_id.id, 'company_id': company.id, 'partner_ref': number, 'origin': 'almaceneselite.com',
            'ea_order_numbers': number, 'ea_state': state, 'ea_from_web': True, 'ea_last_sync': fields.Datetime.now(),
            'order_line': self._ea_web_lines(api_order.get('lineas'), company)})
        if state in ('confirmed', 'shipped'):
            order._ea_confirm_decided()
        ea_note(order, _("Bought on almaceneselite.com: Almacenes Elite order %s.", number))
        order._ea_web_delivery(api_order, permission=permission)
        self._ea_update_from_api(api_order, company)   # shipments and tracking, if any
        return order

    @api.model
    def _ea_fetch_web_order(self, number, company):
        body = self.env['almacenes.elite.api']._ea_get('estado', {'numero': number}, company=company)
        found = [o for o in body.get('pedidos') or [] if o.get('numero') == number]
        if not found:
            raise UserError(_("Almacenes Elite order %s was not found for your account.", number))
        return found[0]

    @api.model
    def _ea_back_from_website(self, pedido=None, direcciones=None, **kwargs):
        """What almaceneselite.com brings back to this Odoo: the orders placed there («S1» or «S1,S2» when part of the order is
        shipped separately). ``direcciones`` (si/no) is the answer given on the website about adding new delivery addresses
        here: it is remembered for the company when it had not chosen yet. Returns the purchase orders created or found."""
        company = self.env.company.sudo()
        permission = {'si': 'add', 'no': 'manual'}.get(str(direcciones or '').strip().lower())
        if permission and not company.ea_web_addresses:
            company.ea_web_addresses = permission
        if not pedido:
            return self.browse()
        numbers = [n.strip()[:30] for n in str(pedido).split(',') if n.strip()][:5]
        api_orders = [self._ea_fetch_web_order(number, company) for number in numbers]
        orders = self.browse()
        done = set()
        for rfq, group in self._ea_group_web_payments(api_orders, company):
            rfq._ea_finish_web_payment(group, permission=permission)
            orders |= rfq
            done.update(o.get('numero') for o in group)
        for api_order in api_orders:
            if api_order.get('numero') not in done:
                orders |= self._ea_import_web_order(api_order['numero'], company=company, api_order=api_order, permission=permission)
        return orders

    @api.model
    def _ea_group_web_payments(self, api_orders, company):
        """Orders placed on the website for an RFQ of this Odoo that was opened there («Order on almaceneselite.com»), grouped by
        RFQ (an order can come as two when part of it ships separately)."""
        vendor = company.ea_partner_id
        groups = {}
        for api_order in api_orders:
            if api_order.get('estado') in ('rechazado', 'cancelado', 'prueba') or not api_order.get('numero'):
                continue
            for key in (api_order.get('solicitud'), api_order.get('ref_cliente')):
                if not key:
                    continue
                rfq = self.search([('company_id', '=', company.id), ('partner_id', 'child_of', vendor.commercial_partner_id.id),
                                   ('name', '=', key), ('ea_state', '=', 'payment'), ('state', 'in', OPEN_STATES)], limit=1)
                if rfq:
                    groups.setdefault(rfq.id, []).append(api_order)
                    break
        return [(self.browse(rid), group) for rid, group in groups.items()]

    def _ea_finish_web_payment(self, api_orders, permission=None):
        """The RFQ that was opened on the website comes back ordered: lines, prices, shipping and order numbers as at Almacenes
        Elite, then confirmed (or left waiting if their company still has to approve it on the website)."""
        self.ensure_one()
        states = [API_STATE.get(o.get('estado'), 'confirmed') for o in api_orders]
        confirmed = all(s in ('confirmed', 'shipped') for s in states)
        self._ea_apply_order_result({'pedidos': api_orders, 'estado': 'confirmado' if confirmed else 'pendiente_aprobacion'}, paid_on_web=True)
        self._ea_web_delivery(api_orders[0], permission=permission or {'si': 'add', 'no': 'manual'}.get(api_orders[0].get('direccion_a_odoo') or ''))
        if confirmed and self.state in ('draft', 'sent', 'to approve'):
            self._ea_confirm_decided()

    # ------------------------------------------------------------------ synchronisation (cron)
    @api.model
    def _cron_ea_sync(self):
        companies = self.env['res.company'].sudo().search([('ea_consent', '=', True), ('ea_api_key', '!=', False)])
        for company in companies:
            try:
                self.with_company(company)._ea_sync_company(company)
                self.env.cr.commit()
            except Exception as exc:   # one company failing never stops the others
                self.env.cr.rollback()
                _logger.warning("Almacenes Elite sync failed for %s: %s", company.name, exc)
                company.write({'ea_last_error': str(exc)[:500]})
                self.env.cr.commit()

    @api.model
    def _ea_sync_company(self, company, quick=False):
        """Orders (status, shipments, orders placed on the website), then invoices. quick=True (every few minutes after opening the
        website from this Odoo): only the orders."""
        company = company.sudo()
        Api = self.env['almacenes.elite.api']
        if not quick:
            company._ea_refresh_account()   # payment method, delivery addresses and shipping rule can change at Almacenes Elite
        since = company.ea_orders_since or (fields.Datetime.now() - timedelta(days=30)).strftime('%Y-%m-%dT%H:%M:%S')
        for _page in range(10):
            body = Api._ea_get('pedidos', {'desde': since, 'limite': 100, 'solo_api': 1}, company=company)
            batch = body.get('pedidos') or []
            done = set()
            for rfq, group in self._ea_group_web_payments(batch, company):
                # ordered on the website and the tab was closed before coming back
                rfq._ea_finish_web_payment(group)
                done.update(o.get('numero') for o in group)
            for api_order in batch:
                if api_order.get('numero') in done:
                    continue
                if self._ea_find_orders(api_order, company):
                    self._ea_update_from_api(api_order, company)
                elif api_order.get('numero') and api_order.get('estado') not in ('rechazado', 'cancelado', 'prueba'):
                    emailed = self._ea_emailed_rfq(api_order, company)
                    if emailed:
                        # this RFQ was sent by email («Send by email» to Almacenes Elite) and they placed it: the same order, not a new one
                        emailed._ea_link_emailed(api_order)
                    else:
                        # bought on almaceneselite.com from this Odoo and not brought back yet (e.g. the tab was closed)
                        self._ea_import_web_order(api_order['numero'], company=company, api_order=api_order)
            since = body.get('siguiente_desde') or since
            if not body.get('hay_mas'):
                break
        if quick:
            company.write({'ea_orders_since': since})
            return True
        vals = {'ea_orders_since': since, 'ea_last_sync': fields.Datetime.now(), 'ea_last_error': False}
        if company.ea_import_bills:
            bills_since = company.ea_bills_since or (fields.Datetime.now() - timedelta(days=30)).strftime('%Y-%m-%dT%H:%M:%S')
            for _page in range(10):
                body = Api._ea_get('facturas', {'desde': bills_since, 'limite': 50}, company=company)
                for invoice in body.get('facturas') or []:
                    self.env['account.move']._ea_import_invoice(invoice, company)
                bills_since = body.get('siguiente_desde') or bills_since
                if not body.get('hay_mas'):
                    break
            vals['ea_bills_since'] = bills_since
        company.write(vals)
        return True

    @api.model
    def _cron_ea_nightly(self):
        """Every night: current prices, stock, expected dates and alternatives of the Almacenes Elite products of this Odoo; RFQs not
        ordered yet take the current prices; and a reminder for the RFQs prepared by the replenishment rules."""
        companies = self.env['res.company'].sudo().search([('ea_consent', '=', True), ('ea_api_key', '!=', False), ('ea_partner_id', '!=', False)])
        for company in companies:
            try:
                self.with_company(company)._ea_nightly_company(company)
                self.env.cr.commit()
            except Exception as exc:   # one company failing never stops the others
                self.env.cr.rollback()
                _logger.warning("Almacenes Elite nightly update failed for %s: %s", company.name, exc)

    @api.model
    def _ea_nightly_company(self, company):
        company = company.sudo()
        if company.ea_refresh_prices:
            self.env['product.product']._ea_refresh_products(company)
            vendor = company.ea_partner_id
            self.search([('company_id', '=', company.id), ('partner_id', 'child_of', vendor.commercial_partner_id.id),
                         ('state', 'in', ('draft', 'sent')), ('ea_state', 'in', ('not_sent', 'rejected'))])._ea_update_rfq_prices()
        return self._ea_remind_replenishment(company)

    @api.model
    def _ea_find_orders(self, api_order, company):
        vendor = company.ea_partner_id
        domain = [('company_id', '=', company.id), ('partner_id', 'child_of', vendor.commercial_partner_id.id)]
        orders = self.search(domain + [('name', 'in', [api_order.get('solicitud') or '-', api_order.get('ref_cliente') or '-']),
                                       ('ea_state', '!=', 'not_sent')])
        if not orders and api_order.get('numero'):
            orders = self.search(domain + self._ea_number_domain(api_order['numero']))
        return orders

    @api.model
    def _ea_emailed_rfq(self, api_order, company):
        """The RFQ of this Odoo that the person sent to Almacenes Elite by email (the usual «Send by email»), placed there as an
        order with this RFQ's number as the customer reference (also when it was confirmed here first)."""
        ref = api_order.get('ref_cliente') or ''
        if not ref:
            return self.browse()
        vendor = company.ea_partner_id
        return self.search([('company_id', '=', company.id), ('partner_id', 'child_of', vendor.commercial_partner_id.id), ('name', '=', ref),
                            ('ea_state', '=', 'not_sent'), ('state', 'in', OPEN_STATES)], limit=1)

    def _ea_link_emailed(self, api_order):
        """An RFQ sent by email and placed by Almacenes Elite: their order number, lines, prices and shipping, and confirmed here
        when it is confirmed there (or waiting for its payment, for accounts that pay by card or PayPal)."""
        self.ensure_one()
        self._ea_apply_order_result({'pedidos': [api_order], 'estado': api_order.get('estado')})
        ea_note(self, _("Almacenes Elite received this RFQ by email and placed it as order %s: this is the same purchase order (no duplicate).",
                        api_order.get('numero')))
        if API_STATE.get(api_order.get('estado'), 'confirmed') in ('confirmed', 'shipped'):
            self._ea_confirm_decided()

    @api.model
    def _ea_number_domain(self, number):
        """Purchase orders whose Almacenes Elite order numbers («S18741» or «S18741, S18742») include exactly this one
        (S1874 must not find S18741)."""
        return ['|', '|', '|', ('ea_order_numbers', '=', number), ('ea_order_numbers', '=like', number + ',%'),
                ('ea_order_numbers', '=like', '%, ' + number), ('ea_order_numbers', '=like', '%, ' + number + ',%')]

    @api.model
    def _ea_update_from_api(self, api_order, company):
        for order in self._ea_find_orders(api_order, company):
            lang = order.user_id.lang or company.partner_id.lang or self.env.user.lang
            order = order.with_context(lang=lang)
            if order.ea_state == 'payment' and order.state in OPEN_STATES and api_order.get('numero') \
                    and api_order.get('estado') not in ('rechazado', 'cancelado', 'prueba', 'pendiente_pago'):
                order._ea_finish_web_payment([api_order])
            state = API_STATE.get(api_order.get('estado'), order.ea_state)
            shipments = api_order.get('entregas') or []
            pending = api_order.get('pendiente') or []
            if state == 'confirmed' and shipments and not pending and all(s.get('estado') == 'enviado' for s in shipments):
                state = 'shipped'
            seen = set((order.ea_shipments_seen or '').split(',')) - {''}
            for ship in shipments:
                if ship.get('estado') == 'enviado' and ship.get('albaran') not in seen:
                    seen.add(ship.get('albaran'))
                    number, link = ship.get('seguimiento') or '', ship.get('url_seguimiento')
                    tracking = (Markup('<a href="%s" target="_blank" rel="noopener">%s</a>') % (link, number)) if link and number else (number or _("No tracking number"))
                    ea_note(order, Markup('%s<br/>%s') % (
                        _("Almacenes Elite shipped %(doc)s with %(carrier)s.", doc=ship.get('albaran'), carrier=ship.get('transportista') or '-'), tracking))
                    order._ea_set_picking_tracking(ship)
            if order.ea_from_web and order.state in ('draft', 'sent', 'to approve'):
                if state in ('confirmed', 'shipped'):
                    order._ea_confirm_decided()
                elif state in ('rejected', 'cancelled'):
                    order.button_cancel()
            order.write({'ea_state': state, 'ea_shipments': {'entregas': shipments, 'pendiente': pending},
                         'ea_shipments_seen': ','.join(sorted(seen)), 'ea_last_sync': fields.Datetime.now()})

    def _ea_set_picking_tracking(self, ship):
        """With the Inventory/Delivery apps installed, the tracking number also goes to the receipt."""
        self.ensure_one()
        if 'picking_ids' not in self._fields or not ship.get('seguimiento'):
            return
        for picking in self.picking_ids.filtered(lambda p: p.state not in ('done', 'cancel')):
            if 'carrier_tracking_ref' in picking._fields and not picking.carrier_tracking_ref:
                picking.carrier_tracking_ref = ship['seguimiento']
