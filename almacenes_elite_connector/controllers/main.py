# -*- coding: utf-8 -*-
from markupsafe import Markup, escape

from odoo import _, http
from odoo.exceptions import AccessError, UserError
from odoo.http import request


class AlmacenesEliteController(http.Controller):

    @http.route('/almacenes_elite/volver', type='http', auth='user', methods=['GET'], sitemap=False)
    def ea_back_from_website(self, pedido=None, direcciones=None, **kwargs):
        """almaceneselite.com sends the buyer back here with the orders placed (pedido=S12345, or S12345,S12346 when part of it
        ships separately). Without it (they left without buying) it opens the purchase orders. ``direcciones`` (si/no) is the
        answer given on the website about adding new delivery addresses to this Odoo."""
        PurchaseOrder = request.env['purchase.order']
        try:
            orders = PurchaseOrder._ea_back_from_website(pedido=pedido, direcciones=direcciones)
        except (UserError, AccessError) as exc:
            body = Markup('<p>%s</p><p><a href="/web#model=purchase.order&amp;view_type=list">%s</a></p>') % (exc.args[0] if exc.args else exc, _("Back to Purchase"))
            return request.make_response(
                Markup('<!DOCTYPE html><html><head><meta charset="utf-8"><title>Almacenes Elite</title></head>'
                       '<body style="font-family:sans-serif;max-width:640px;margin:48px auto;padding:0 16px">'
                       '<h2>%s</h2>%s</body></html>') % (escape(_("Almacenes Elite")), body),
                headers=[('Content-Type', 'text/html; charset=utf-8')])
        if len(orders) == 1:
            return request.redirect('/web#id=%d&model=purchase.order&view_type=form' % orders.id)
        return request.redirect('/web#model=purchase.order&view_type=list')

    @http.route('/almacenes_elite/alta', type='http', auth='user', methods=['GET'], sitemap=False)
    def ea_signup_confirmed(self, **kwargs):
        """«Back to my Odoo» on the almaceneselite.com page where an existing customer confirmed the connection: connect now
        (instead of waiting a few minutes for the automatic check) and open the settings."""
        companies = request.env['res.company'].sudo().search([('id', 'in', request.env.user.company_ids.ids), ('ea_signup_date', '!=', False)])
        for company in companies:
            try:
                company._ea_check_signup()
            except UserError:
                pass   # no internet for a moment: the automatic check connects it later
        if request.env.user.has_group('base.group_system'):
            return request.redirect('/web#action=purchase.action_purchase_configuration')
        return request.redirect('/web#model=purchase.order&view_type=list')
