# -*- coding: utf-8 -*-
"""Tests with a fake Almacenes Elite API (no network). Orders are always placed on almaceneselite.com: the app never sends an
order through the API (the fake API rejects it like the real one, and every test checks it was never even tried)."""
import json
from datetime import timedelta
from unittest.mock import patch

import requests

from odoo import fields
from odoo.exceptions import UserError
from odoo.tools import float_round
from odoo.tests import TransactionCase, tagged

ITEMS = {
    '080142': {'ref': '080142', 'ean': '4902505163165', 'nombre': 'Bolígrafo Pilot G-2 azul', 'marca': 'Pilot', 'precio': 0.9094, 'iva': 21.0,
               'multiplo': 12, 'minimo': 12, 'stock': {'estado': 'en_stock', 'texto': 'En stock', 'disponible': 500}, 'imagen': '', 'url': ''},
    '080143': {'ref': '080143', 'ean': '4902505163172', 'nombre': 'Bolígrafo Pilot G-2 negro', 'marca': 'Pilot', 'precio': 0.9094, 'iva': 21.0,
               'multiplo': 12, 'minimo': 12, 'stock': {'estado': 'en_stock', 'texto': 'En stock', 'disponible': 300}, 'imagen': '', 'url': ''},
    '630012': {'ref': '630012', 'ean': '8000825967559', 'nombre': 'Tiza color Robercolor 100 uds', 'marca': 'Robercolor', 'precio': 6.777, 'iva': 21.0,
               'multiplo': 1, 'minimo': 0, 'stock': {'estado': 'en_stock', 'texto': 'En stock', 'disponible': 40}, 'imagen': '', 'url': ''},
}


class FakeResponse:
    def __init__(self, status, body, ctype='application/json'):
        self.status_code, self._body, self.headers = status, body, {'Content-Type': ctype}
        self.content = body if isinstance(body, bytes) else json.dumps(body).encode()

    def json(self):
        if isinstance(self._body, bytes):
            raise ValueError('not json')
        return self._body


class FakeApi:
    def __init__(self):
        self.orders, self.invoices, self.calls = [], [], []
        self.last_params = {}
        self.items = {}
        self.reset()

    def reset(self):
        self.orders.clear()
        self.invoices = []
        self.calls.clear()
        self.last_params.clear()
        self.items = {k: dict(v, stock=dict(v['stock'])) for k, v in ITEMS.items()}
        self.pago = 'habitual'          # 'web' = the account pays by card or PayPal on the website
        self.direcciones = [{'id': 7, 'nombre': 'Oficina', 'defecto': True}]
        self.enlace = 'pendiente'       # existing customer linking: pendiente / revision / lista / rechazada
        self.portes = {'regla': 'sector', 'minimo': 80.0, 'importe': 4.0}

    def _lines(self, quantities):
        lines = []
        for ref, wanted in quantities.items():
            item = self.items[ref]
            qty = max(wanted, item['multiplo'], item['minimo'])
            if item['multiplo'] > 1 and qty % item['multiplo']:
                qty = (qty // item['multiplo'] + 1) * item['multiplo']
            lines.append({'ref': ref, 'ean': item['ean'], 'nombre': item['nombre'], 'cantidad': float(qty), 'precio': item['precio'], 'descuento': 0.0,
                          'subtotal': float_round(qty * item['precio'], 2), 'iva': 21.0, 'impuestos': [21.0], 'tipo': 'producto'})
        lines.append({'ref': 'Delivery_007', 'ean': '', 'nombre': 'Entrega estándar', 'cantidad': 1.0, 'precio': 4.0, 'descuento': 0.0, 'subtotal': 4.0,
                      'iva': 21.0, 'impuestos': [21.0], 'tipo': 'portes'})
        return lines

    def web_order(self, number, quantities, state='confirmado', ref_cliente='', direccion=None, direccion_a_odoo='', pendiente=None):
        """An order placed on almaceneselite.com from the customer's Odoo (an RFQ of that Odoo behind it when it was opened from one)."""
        lines = self._lines(quantities)
        base = float_round(sum(x['subtotal'] for x in lines), 2)   # rounded like Odoo (half up), as our invoices
        order = {'numero': number, 'ref_cliente': ref_cliente, 'solicitud': ref_cliente, 'estado': state, 'base': base, 'iva': float_round(base * 0.21, 2),
                 'total': float_round(base + float_round(base * 0.21, 2), 2), 'moneda': 'EUR', 'lineas': lines, 'entregas': [],
                 'pendiente': pendiente or [], 'direccion_entrega': direccion, 'direccion_a_odoo': direccion_a_odoo}
        self.orders.append(order)
        return order

    def get(self, url, params=None, headers=None, timeout=None):
        op = params['op']
        self.calls.append(('GET', op))
        self.last_params[op] = dict(params)
        if op == 'entrar':
            return FakeResponse(200, {'ok': True, 'url': 'https://example.test/website/action/elite-entrar?t=et_' + 'b' * 64, 'modo': 'pedido'})
        if op == 'estado':
            found = [o for o in self.orders if o['numero'] == params.get('numero')]
            return FakeResponse(200 if found else 404, {'ok': bool(found), 'pedidos': found, 'error': {'codigo': 'no_encontrado', 'mensaje': 'No'}})
        if op == 'sesion':
            return FakeResponse(200, {'ok': True, 'csrf_token': 'tok'})
        if op == 'cuenta':
            return FakeResponse(200, {'ok': True, 'cliente': {'nombre': 'Cliente de prueba', 'pago': self.pago},
                                      'proveedor': {'nombre': 'ALMACENES ELITE, S.L.', 'nif': 'ESB46126314', 'pais': 'ES', 'web': 'https://almaceneselite.com'},
                                      'direcciones': self.direcciones, 'portes': self.portes, 'pedidos_en': 'web'})
        if op == 'solicitud_pago':
            return FakeResponse(200, {'ok': True, 'estado': 'recibida', 'mensaje': 'Lo estudiamos y te llamamos.'})
        if op == 'productos':
            refs = [r for r in (params.get('refs') or '').split(',') if r]
            found = [dict(self.items[r]) for r in refs if r in self.items]
            if str(params.get('disponibilidad')) == '1':
                for it in found:
                    if it['stock']['estado'] == 'sin_stock':
                        it['prevista'] = '2026-10-25'
                        alt = self.items['080143']
                        if it['ref'] != '080143' and alt['stock']['estado'] == 'en_stock':
                            it['alternativa'] = {k: alt[k] for k in ('ref', 'ean', 'nombre', 'marca', 'precio', 'iva', 'multiplo', 'minimo', 'stock', 'url')}
            return FakeResponse(200, {'ok': True, 'productos': found, 'total': len(found), 'no_encontrados': [r for r in refs if r not in self.items]})
        if op == 'pedidos':
            return FakeResponse(200, {'ok': True, 'pedidos': self.orders, 'siguiente_desde': '2099-01-01T00:00:00', 'hay_mas': False})
        if op == 'facturas':
            return FakeResponse(200, {'ok': True, 'facturas': self.invoices, 'siguiente_desde': '2099-01-01T00:00:00', 'hay_mas': False})
        if op == 'factura_pdf':
            return FakeResponse(200, b'%PDF-1.4 fake', ctype='application/pdf')
        return FakeResponse(400, {'ok': False, 'error': {'codigo': 'operacion', 'mensaje': 'op'}})

    def post(self, url, data=None, headers=None, timeout=None):
        op = data['op']
        self.calls.append(('POST', op))
        if op == 'alta':
            datos = json.loads(data['datos'])
            self.last_params['alta'] = datos
            if datos.get('nif') == '00000000T':
                return FakeResponse(409, {'ok': False, 'estado': 'ya_cliente', 'error': {'codigo': 'ya_cliente', 'mensaje': 'Ya eres cliente.'}})
            if datos.get('nif') == 'B12345674':   # already a customer, with an email on file: confirmation by email
                return FakeResponse(200, {'ok': True, 'estado': 'confirmar', 'vale': 'av_' + 'f' * 64, 'enviado_a': 'c•••@cliente.test',
                                          'mensaje': 'Ya eres cliente: te hemos enviado un correo a c•••@cliente.test.'})
            if datos.get('nif') == 'B87654323':   # already a customer, archived or without email: Almacenes Elite checks it
                return FakeResponse(200, {'ok': True, 'estado': 'revision', 'vale': 'av_' + '9' * 64, 'enviado_a': '',
                                          'mensaje': 'Lo revisamos y te llamamos.'})
            return FakeResponse(200, {'ok': True, 'estado': 'alta', 'clave': 'ea_' + 'd' * 64, 'tarifa': 'Por defecto (9)', 'pago': 'web',
                                      'mensaje': 'Ya eres cliente de Almacenes Élite.'})
        if op == 'alta_estado':
            if not (data.get('vale') or '').startswith('av_'):
                return FakeResponse(404, {'ok': False, 'error': {'codigo': 'vale', 'mensaje': 'No encontramos esta solicitud.'}})
            if self.enlace == 'lista':
                return FakeResponse(200, {'ok': True, 'estado': 'lista', 'clave': 'ea_' + 'e' * 64, 'cliente': 'Cliente de prueba',
                                          'tarifa': 'TARIFA EMPRESAS (12)', 'pago': 'habitual', 'mensaje': 'Listo.'})
            if self.enlace == 'rechazada':
                return FakeResponse(410, {'ok': False, 'error': {'codigo': 'rechazada', 'mensaje': 'La conexión no se ha confirmado.'}})
            return FakeResponse(200, {'ok': True, 'estado': self.enlace, 'enviado_a': 'c•••@cliente.test' if self.enlace == 'pendiente' else ''})
        if op == 'entrar':
            # an RFQ of this Odoo opens on the website with its products in a new cart
            self.last_params['entrar'] = dict(data)
            if any(ln['ref'] not in self.items for ln in json.loads(data['lineas'])):
                return FakeResponse(409, {'ok': False, 'errores': [{'ref': 'x', 'motivo': 'No existe'}]})
            return FakeResponse(200, {'ok': True, 'url': 'https://example.test/website/action/elite-entrar?t=et_' + 'c' * 64})
        # like the real API: orders are placed on the website, never sent by the app
        return FakeResponse(403, {'ok': False, 'error': {'codigo': 'pedido_en_web', 'mensaje': 'Los pedidos se hacen en almaceneselite.com.'}})


@tagged('post_install', '-at_install')
class TestAlmacenesEliteConnector(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.write({'ea_consent': True, 'ea_api_key': 'ea_' + 'a' * 48, 'ea_api_url': 'https://example.test',
                           'ea_web_addresses': False, 'po_double_validation': 'one_step', 'ea_replenish_user_id': False})
        cls.api = FakeApi()
        cls.patches = [patch.object(requests.Session, 'get', lambda s, *a, **k: cls.api.get(*a, **k)),
                       patch.object(requests.Session, 'post', lambda s, *a, **k: cls.api.post(*a, **k))]
        for p in cls.patches:
            p.start()
        cls.company._ea_connect()

    @classmethod
    def tearDownClass(cls):
        for p in cls.patches:
            p.stop()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.api.reset()   # the fake API lives for the whole class: every test starts with no orders and no invoices

    def tearDown(self):
        self.assertNotIn(('POST', 'pedido'), self.api.calls, "the app never sends an order: it is placed on the website")
        super().tearDown()

    # ------------------------------------------------------------------ helpers
    def _product(self, ref):
        return self.env['product.product']._ea_get_or_create(dict(self.api.items[ref], minimo=0), self.company)

    def _rfq(self, quantities, **vals):
        """An RFQ of this Odoo for Almacenes Elite (products bought before: they have the Almacenes Elite code)."""
        lines = [(0, 0, {'product_id': self._product(ref).id, 'product_qty': qty, 'price_unit': self.api.items[ref]['precio']})
                 for ref, qty in quantities.items()]
        return self.env['purchase.order'].create(dict({'partner_id': self.company.ea_partner_id.id, 'order_line': lines}, **vals))

    def _exact_prices(self):
        """Odoo 17 rounds unit prices to 2 decimals unless the precision is raised (what the settings button does)."""
        settings = self.env['res.config.settings'].create({})
        if settings.ea_price_rounding:
            settings.action_ea_price_digits()
            self.addCleanup(self.env.registry.clear_cache)

    def _user(self, name, group_xmlid):
        groups = 'group_ids' if 'group_ids' in self.env['res.users']._fields else 'groups_id'
        return self.env['res.users'].create({'name': name, 'login': name.lower().replace(' ', '_') + '@ea.test', 'company_id': self.company.id,
                                             'company_ids': [(6, 0, [self.company.id])], groups: [(6, 0, [self.env.ref(group_xmlid).id])]})

    # ------------------------------------------------------------------ connection
    def test_connect_creates_vendor_once(self):
        vendor = self.company.ea_partner_id
        self.assertEqual(vendor.vat, 'ESB46126314')
        self.company._ea_connect()
        self.assertEqual(self.env['res.partner'].search_count([('vat', '=', 'ESB46126314')]), 1)
        self.assertEqual((self.company.ea_shipping_rule, self.company.ea_shipping_min, self.company.ea_shipping_amount), ('sector', 80.0, 4.0))

    # ------------------------------------------------------------------ shopping on almaceneselite.com
    def test_shop_on_website_opens_signed_link(self):
        action = self.env['purchase.order'].action_ea_punchout()
        self.assertEqual(action['type'], 'ir.actions.act_url')
        self.assertIn('elite-entrar?t=et_', action['url'])
        params = self.api.last_params['entrar']
        self.assertTrue(params['volver'].endswith('/almacenes_elite/volver'))
        self.assertEqual(params['modo'], 'pedido', "always ordering on the website (there is no cart mode any more)")

    def test_web_order_comes_back_confirmed_and_never_twice(self):
        self.api.web_order('S90050', {'080142': 12, '630012': 2})
        PO = self.env['purchase.order']
        order = PO._ea_import_web_order('S90050')
        self.assertEqual((order.state, order.ea_from_web, order.partner_ref, order.ea_state), ('purchase', True, 'S90050', 'confirmed'))
        freight = self.company.ea_freight_product_id
        self.assertEqual(len(order.order_line.filtered(lambda l: l.product_id != freight)), 2)
        self.assertEqual(order.order_line.filtered(lambda l: l.product_id == freight).price_unit, 4.0)
        self.assertEqual(PO._ea_import_web_order('S90050'), order)
        PO._ea_sync_company(self.company)
        self.assertEqual(PO.search_count([('ea_order_numbers', '=', 'S90050')]), 1)

    def test_closed_tab_order_arrives_with_the_sync(self):
        self.api.web_order('S90051', {'080142': 24})
        self.env['purchase.order']._ea_sync_company(self.company)
        order = self.env['purchase.order'].search([('ea_order_numbers', '=', 'S90051')])
        self.assertEqual((len(order), order.state), (1, 'purchase'))

    def test_web_order_waiting_for_approval_is_confirmed_later(self):
        api_order = self.api.web_order('S90052', {'630012': 3}, state='pendiente_aprobacion')
        order = self.env['purchase.order']._ea_import_web_order('S90052')
        self.assertIn(order.state, ('draft', 'sent'))
        self.assertEqual(order.ea_state, 'approval')
        api_order['estado'] = 'confirmado'
        self.env['purchase.order']._ea_sync_company(self.company)
        self.assertEqual(order.state, 'purchase')

    def test_order_number_matches_exactly(self):
        self.api.web_order('S918741', {'080142': 12})
        self.env['purchase.order']._ea_import_web_order('S918741')
        self.api.web_order('S91874', {'630012': 1})
        order = self.env['purchase.order']._ea_import_web_order('S91874')
        self.assertEqual(order.ea_order_numbers, 'S91874', "S91874 must not be mistaken for S918741")

    def test_back_with_two_orders_when_part_ships_separately(self):
        self.api.web_order('S90060', {'080142': 12})
        self.api.web_order('S90061', {'630012': 2})
        orders = self.env['purchase.order']._ea_back_from_website(pedido='S90060,S90061')
        self.assertEqual(sorted(orders.mapped('partner_ref')), ['S90060', 'S90061'])
        self.assertEqual(set(orders.mapped('state')), {'purchase'})
        self.assertEqual(self.env['purchase.order']._ea_back_from_website(pedido='S90060, S90061'), orders, "never twice")
        self.assertFalse(self.env['purchase.order']._ea_back_from_website(), "left without buying: nothing created")

    def test_b2b_portal_opens_signed_in(self):
        action = self.env['purchase.order'].action_ea_portal()
        self.assertEqual(action['type'], 'ir.actions.act_url')
        self.assertEqual(self.api.last_params['entrar']['destino'], 'portal')

    # ------------------------------------------------------------------ an RFQ of this Odoo, ordered on the website
    def test_rfq_opens_in_the_web_cart_and_comes_back_confirmed(self):
        self._exact_prices()   # Odoo 17 rounds unit prices to 2 decimals unless the setting gives them 4
        order = self._rfq({'080142': 12, '630012': 2})
        action = order.action_ea_order_on_web()
        self.assertEqual(action['type'], 'ir.actions.act_url')
        sent = self.api.last_params['entrar']
        self.assertEqual((sent['ref_cliente'], sent['modo']), (order.name, 'pedido'))
        self.assertEqual(sorted(ln['ref'] for ln in json.loads(sent['lineas'])), ['080142', '630012'])
        self.assertEqual((order.state, order.ea_state), ('draft', 'payment'), "nothing is ordered until it is placed on the website")
        self.api.web_order('S90070', {'080142': 12, '630012': 2}, ref_cliente=order.name)
        back = self.env['purchase.order']._ea_back_from_website(pedido='S90070')
        self.assertEqual(back, order, "the same RFQ, never a second purchase order")
        self.assertEqual((order.state, order.ea_order_numbers, order.ea_state), ('purchase', 'S90070', 'confirmed'))
        self.assertAlmostEqual(order.amount_total, self.api.orders[-1]['total'], delta=0.02)
        self.assertEqual(self.env['purchase.order'].search_count([('ea_order_numbers', '=', 'S90070')]), 1)

    def test_rfq_confirmed_here_first_is_still_one_order(self):
        order = self._rfq({'630012': 3})
        order.button_confirm()
        self.assertEqual((order.state, order.ea_state), ('purchase', 'not_sent'), "confirming in Odoo orders nothing at Almacenes Elite")
        order.action_ea_order_on_web()
        self.api.web_order('S90071', {'630012': 3}, ref_cliente=order.name)
        self.env['purchase.order']._ea_sync_company(self.company)   # (tab closed: it arrives with the sync)
        self.assertEqual((order.ea_order_numbers, order.ea_state), ('S90071', 'confirmed'))
        self.assertEqual(self.env['purchase.order'].search_count([('ea_order_numbers', '=', 'S90071')]), 1)

    def test_closed_tab_order_arrives_in_minutes(self):
        """After opening the website from Odoo its orders are checked every few minutes (only the orders), not only every hour."""
        PO = self.env['purchase.order']
        order = self._rfq({'630012': 3})
        self.company.ea_web_open_until = False
        self.assertNotIn(self.company, PO._ea_quick_sync_companies(), "nothing to check until the website is opened from Odoo")
        order.action_ea_order_on_web()
        self.assertIn(self.company, PO._ea_quick_sync_companies())
        self.api.web_order('S90073', {'630012': 3}, ref_cliente=order.name)
        self.api.calls.clear()
        PO._ea_sync_company(self.company, quick=True)
        self.assertEqual((order.state, order.ea_order_numbers, order.ea_state), ('purchase', 'S90073', 'confirmed'))
        self.assertIn(('GET', 'pedidos'), self.api.calls)
        self.assertFalse({('GET', 'cuenta'), ('GET', 'facturas')} & set(self.api.calls), "the quick check only asks for the orders")
        self.company.ea_web_open_until = fields.Datetime.now() - timedelta(minutes=1)
        self.assertNotIn(self.company, PO._ea_quick_sync_companies(), "hours later it stops")
        PO.action_ea_punchout()
        self.assertIn(self.company, PO._ea_quick_sync_companies(), "shopping from the menu counts too")

    def test_products_added_on_the_website_come_back_too(self):
        order = self._rfq({'080142': 12})
        order.action_ea_order_on_web()
        self.api.web_order('S90072', {'080142': 12, '630012': 1}, ref_cliente=order.name)
        self.env['purchase.order']._ea_back_from_website(pedido='S90072')
        refs = sorted(order._ea_line_ref(l) for l in order.order_line if l.product_id != self.company.ea_freight_product_id)
        self.assertEqual(refs, ['080142', '630012'], "what was added to the cart on the website is on the purchase order too")

    def test_rfq_without_almacenes_elite_codes_cannot_open(self):
        product = self.env['product.product'].create({'name': 'Grapadora sin código', 'type': 'consu', 'purchase_ok': True})
        order = self.env['purchase.order'].create({'partner_id': self.company.ea_partner_id.id,
                                                   'order_line': [(0, 0, {'product_id': product.id, 'product_qty': 1, 'price_unit': 1})]})
        with self.assertRaises(UserError):
            order.action_ea_order_on_web()
        self.assertEqual(order.ea_state, 'not_sent')

    def test_cost_center_travels_to_the_web_cart(self):
        plan = self.env['account.analytic.plan'].create({'name': 'Departamentos'})
        adm = self.env['account.analytic.account'].create({'name': 'Administración', 'code': 'VAL-ADM', 'plan_id': plan.id})
        mkt = self.env['account.analytic.account'].create({'name': 'Marketing', 'code': 'MKT', 'plan_id': plan.id})
        order = self._rfq({'080142': 12, '630012': 1})
        self.assertEqual(order._ea_cost_center(), {}, "without analytic accounts nothing extra travels")
        order.order_line.write({'analytic_distribution': {str(adm.id): 100}})
        order.action_ea_order_on_web()
        self.assertEqual(json.loads(self.api.last_params['entrar']['centro_coste']),
                         {'codigo': 'VAL-ADM', 'nombre': 'Administración', 'etiqueta': 'VAL-ADM — Administración'})
        order.order_line.filtered(lambda l: order._ea_line_ref(l) == '630012').write({'analytic_distribution': {str(mkt.id): 100}})
        cc = order._ea_cost_center()
        self.assertEqual(list(cc), ['etiqueta'], "several cost centers: only the label (no single one is chosen)")
        self.assertTrue(cc['etiqueta'].startswith('VAL-ADM') and 'MKT' in cc['etiqueta'] and '%' in cc['etiqueta'])

    def test_rfq_sent_by_email_is_the_same_order(self):
        order = self._rfq({'630012': 2})
        # «Send by email» to pedidos@almaceneselite.com and Almacenes Elite places it («Pasar a pedido web»): same purchase order
        self.api.web_order('S90099', {'630012': 2}, ref_cliente=order.name)
        before = self.env['purchase.order'].search_count([])
        self.env['purchase.order']._ea_sync_company(self.company)
        self.assertEqual(self.env['purchase.order'].search_count([]), before, "no second purchase order")
        self.assertEqual((order.ea_order_numbers, order.state, order.ea_state), ('S90099', 'purchase', 'confirmed'))
        # a card or PayPal account: the emailed RFQ waits for its payment instead of being confirmed
        other = self._rfq({'630012': 3})
        self.api.web_order('S90100', {'630012': 3}, state='pendiente_pago', ref_cliente=other.name)
        self.env['purchase.order']._ea_sync_company(self.company)
        self.assertEqual((other.ea_order_numbers, other.state, other.ea_state), ('S90100', 'draft', 'payment'))

    # ------------------------------------------------------------------ improvements: prices, stock, alternatives, shipping
    def test_nightly_prices_stock_and_alternatives(self):
        order = self._rfq({'080142': 12, '630012': 2})
        self.api.items['630012']['precio'] = 6.9
        self.api.items['080142']['stock'] = {'estado': 'sin_stock', 'texto': 'Sin stock ahora mismo', 'disponible': 0}
        self.env['purchase.order']._ea_nightly_company(self.company)
        tiza = order.order_line.filtered(lambda l: order._ea_line_ref(l) == '630012')
        self.assertAlmostEqual(tiza.price_unit, 6.9, places=4, msg="RFQs not ordered yet take the current price")
        info = order._ea_seller(order.order_line.filtered(lambda l: order._ea_line_ref(l) == '080142').product_id)
        self.assertEqual((info.ea_stock_state, str(info.ea_expected_date), info.ea_alternative['ref']), ('sin_stock', '2026-10-25', '080143'))
        self.assertIn('080143', order.ea_availability)
        self.assertTrue(order.ea_has_alternatives)
        order.action_ea_use_alternatives()
        self.assertEqual(sorted(order._ea_line_ref(l) for l in order.order_line), ['080143', '630012'])
        self.assertEqual(order.order_line.filtered(lambda l: order._ea_line_ref(l) == '080143').product_qty, 12, "same quantity")
        self.assertFalse(order.ea_has_alternatives)

    def test_update_prices_and_stock_from_the_rfq(self):
        order = self._rfq({'630012': 2})
        self.api.items['630012']['precio'] = 7.1
        action = order.action_ea_refresh()
        self.assertEqual(action['tag'], 'display_notification')
        self.assertAlmostEqual(order.order_line.price_unit, 7.1, places=4)
        self.assertIn(('GET', 'productos'), self.api.calls)

    def test_shipping_hint(self):
        self._exact_prices()
        order = self._rfq({'630012': 2})   # 2 × 6.777 = 13.55 before VAT, minimum 80
        self.assertIn('66,45', order.ea_shipping_hint.replace('.', ','), "how much is missing to avoid shipping costs")
        big = self._rfq({'630012': 12})    # 81.32
        self.assertNotIn('more', big.ea_shipping_hint)
        self.api.portes = {'regla': 'sin', 'minimo': 0.0, 'importe': 4.0}
        self.company._ea_refresh_account()
        order.invalidate_recordset(['ea_shipping_hint'])
        self.assertFalse(order.ea_shipping_hint, "no minimum: nothing to say")

    def test_replenishment_reminder_once(self):
        buyer = self._user('Compras EA', 'purchase.group_purchase_user')
        self.company.ea_replenish_user_id = buyer
        order = self._rfq({'630012': 6}, origin='OP/00007')
        manual = self._rfq({'630012': 1})
        PO = self.env['purchase.order']
        self.assertEqual(PO._ea_remind_replenishment(self.company), 1)
        todo = order.activity_ids
        self.assertEqual((len(todo), todo.user_id), (1, buyer))
        self.assertFalse(manual.activity_ids, "only the RFQs prepared by the replenishment rules")
        self.assertEqual(PO._ea_remind_replenishment(self.company), 0, "once per RFQ")
        self.assertEqual(order.state, 'draft', "nothing is ordered by itself")

    def test_pending_items_with_expected_date(self):
        self.api.web_order('S90090', {'080142': 24}, pendiente=[{'ref': '080142', 'cantidad': 12, 'prevista': '2026-10-25'}])
        order = self.env['purchase.order']._ea_import_web_order('S90090')
        self.assertIn('080142', order.ea_tracking)
        self.assertIn('2026', order.ea_tracking)

    def test_return_or_claim_opens_my_returns(self):
        self.api.web_order('S90091', {'630012': 1})
        order = self.env['purchase.order']._ea_import_web_order('S90091')
        action = order.action_ea_return()
        self.assertEqual(action['type'], 'ir.actions.act_url')
        self.assertEqual((self.api.last_params['entrar']['destino'], self.api.last_params['entrar']['pedido']), ('devolucion', 'S90091'))

    def test_see_it_before_signing_up(self):
        settings = self.env['res.config.settings'].create({})
        self.assertTrue(settings.action_ea_open_shop()['url'].endswith('/shop'))
        action = settings.action_ea_howto()
        self.assertEqual(action['res_model'], 'almacenes.elite.howto')
        wizard = self.env['almacenes.elite.howto'].create({})
        self.assertEqual(wizard.action_signup()['res_model'], 'almacenes.elite.signup')
        self.assertFalse([c for c in self.api.calls if c[1] not in ('cuenta',)], "seeing it sends nothing")

    # ------------------------------------------------------------------ vendor bills
    def _import_bill(self, number, quantities, with_vat=True):
        api_order = self.api.web_order('S9%05d' % (len(self.api.orders) + 200), quantities)
        order = self.env['purchase.order']._ea_import_web_order(api_order['numero'])
        invoice = {'numero': number, 'tipo': 'factura', 'fecha': '2026-10-17', 'vencimiento': '2026-11-16', 'total': api_order['total'],
                   'moneda': 'EUR', 'pedidos': [api_order['numero']], 'refs_cliente': [], 'pdf': True,
                   'lineas': [dict(l, pedido=api_order['numero']) for l in api_order['lineas']]}
        if with_vat:
            invoice.update(base=api_order['base'], iva=api_order['iva'])
        self.api.invoices = [invoice]
        self.env['purchase.order']._ea_sync_company(self.company)
        return self.env['account.move'].search([('ea_invoice_number', '=', number)]), api_order, order

    def test_invoice_becomes_linked_bill_once(self):
        bills, api_order, order = self._import_bill('FV/2026/00001', {'080142': 12})
        self.env['purchase.order']._ea_sync_company(self.company)
        bills = self.env['account.move'].search([('ea_invoice_number', '=', 'FV/2026/00001')])
        self.assertEqual(len(bills), 1, "never imported twice")
        self.assertEqual(bills.move_type, 'in_invoice')
        product_line = bills.invoice_line_ids.filtered(lambda l: l.product_id == order.order_line[0].product_id)
        self.assertEqual(product_line.purchase_line_id, order.order_line[0])
        self.assertTrue(bills.message_main_attachment_id)

    def test_bill_keeps_invoice_vat_rounding(self):
        self._exact_prices()
        # 12 × 0.9094 + 14 × 6.777 + 4.00 shipping: VAT 23.05 rounded per line in this Odoo, 23.06 on the Almacenes Elite invoice
        self.company.tax_calculation_rounding_method = 'round_per_line'
        bill, api_order, _order = self._import_bill('FV/2026/00002', {'080142': 12, '630012': 14})
        self.assertAlmostEqual(api_order['iva'], 23.06, places=2)
        self.assertAlmostEqual(bill.amount_untaxed, api_order['base'], places=2)
        self.assertAlmostEqual(bill.amount_tax, 23.06, places=2, msg="a cent of rounding: the bill keeps the invoice VAT")
        self.assertAlmostEqual(bill.amount_total, api_order['total'], places=2)
        self.assertFalse(any('Check this bill' in (m.body or '') for m in bill.message_ids))

    def test_bill_does_not_hide_real_differences(self):
        self._exact_prices()
        self.company.tax_calculation_rounding_method = 'round_per_line'
        bill, _api_order, _order = self._import_bill('FV/2026/00003', {'080142': 12, '630012': 14}, with_vat=False)
        base, tax = bill.amount_untaxed, bill.amount_tax
        self.assertAlmostEqual(tax, 23.05, places=2, msg="without the invoice VAT nothing is changed")
        self.assertFalse(bill._ea_align_tax_rounding({'base': base, 'iva': round(tax + 1.0, 2)}), "more than rounding: nothing is forced")
        self.assertFalse(bill._ea_align_tax_rounding({'base': base + 1.0, 'iva': tax}), "different untaxed amounts: nothing is forced")
        self.assertTrue(bill._ea_align_tax_rounding({'base': base, 'iva': round(tax + 0.01, 2)}))
        self.assertAlmostEqual(bill.amount_tax, round(tax + 0.01, 2), places=2)
        self.assertAlmostEqual(bill.amount_total, round(base + tax + 0.01, 2), places=2)

    # ------------------------------------------------------------------ payment method
    def test_request_another_payment_method(self):
        self.api.pago = 'web'
        self.company._ea_connect()
        self.assertEqual(self.company.ea_payment_mode, 'web')
        settings = self.env['res.config.settings'].create({})
        action = settings.action_ea_request_payment_change()
        self.assertEqual(action['tag'], 'display_notification')
        self.assertTrue(self.company.ea_payment_request_date)
        self.assertEqual(self.api.last_params['solicitud_pago']['usuario'], self.env.user.name)

    # ------------------------------------------------------------------ new delivery addresses
    NEW_ADDRESS = {'id': 99, 'nombre': 'Delegación Castellón', 'calle': 'Avenida del Mar, 5', 'calle2': '', 'cp': '12001',
                   'ciudad': 'Castellón de la Plana', 'provincia': '', 'pais': 'ES', 'telefono': '964 000 000'}

    def test_new_delivery_address_added_when_allowed(self):
        self.company.ea_web_addresses = 'add'
        self.api.web_order('S90080', {'630012': 1}, direccion=self.NEW_ADDRESS)
        order = self.env['purchase.order']._ea_back_from_website(pedido='S90080')
        contact = self.env['res.partner'].search([('parent_id', '=', self.company.partner_id.id), ('street', '=', 'Avenida del Mar, 5')])
        self.assertEqual((len(contact), contact.type), (1, 'delivery'))
        self.assertEqual(order.ea_address_choice, '99')
        self.assertFalse(order.ea_new_address)
        self.api.web_order('S90081', {'630012': 1}, direccion=self.NEW_ADDRESS)
        self.env['purchase.order']._ea_back_from_website(pedido='S90081')
        self.assertEqual(self.env['res.partner'].search_count([('parent_id', '=', self.company.partner_id.id), ('street', '=', 'Avenida del Mar, 5')]), 1,
                         "never twice")

    def test_new_delivery_address_left_to_the_person(self):
        self.assertFalse(self.company.ea_web_addresses)
        self.api.web_order('S90082', {'630012': 1}, direccion=self.NEW_ADDRESS)
        order = self.env['purchase.order']._ea_back_from_website(pedido='S90082', direcciones='no')
        self.assertEqual(self.company.ea_web_addresses, 'manual', "the answer given on the website is remembered")
        self.assertIn('Avenida del Mar, 5', order.ea_new_address_text)
        self.assertTrue(order.activity_ids, "a to-do reminds them to add it")
        self.assertFalse(self.env['res.partner'].search([('street', '=', 'Avenida del Mar, 5')]))
        order.action_ea_add_address()
        self.assertTrue(self.env['res.partner'].search([('parent_id', '=', self.company.partner_id.id), ('street', '=', 'Avenida del Mar, 5')]))
        self.assertFalse(order.ea_new_address)
        self.assertFalse(order.activity_ids)

    def test_known_addresses_are_not_asked_about(self):
        self.company.partner_id.write({'street': 'Calle Mayor, 1', 'zip': '46001'})
        self.api.direcciones = [{'id': 7, 'nombre': 'Oficina', 'calle': 'Calle Mayor 1', 'cp': '46001', 'defecto': True},
                                {'id': 8, 'nombre': 'Almacén', 'calle': 'Calle Nueva 9', 'cp': '46002'}]
        self.company._ea_connect()
        self.env['purchase.order'].action_ea_punchout()
        self.assertEqual(self.api.last_params['entrar']['dir_conocidas'], '7', "only addresses this Odoo already has")
        self.api.web_order('S90083', {'630012': 1}, direccion=dict(self.api.direcciones[0], calle='Calle Mayor, 1'))
        order = self.env['purchase.order']._ea_back_from_website(pedido='S90083')
        self.assertFalse(order.ea_new_address)
        self.assertFalse(order.activity_ids)

    # ------------------------------------------------------------------ becoming a customer from Odoo
    def test_signup_from_odoo_fills_the_key_and_connects(self):
        self.company.write({'ea_api_key': False, 'ea_consent': False, 'ea_account_name': False,
                            'vat': 'ESB46126314', 'street': 'Calle Mayor, 1', 'zip': '46001', 'city': 'Valencia'})
        self.api.pago = 'web'
        wizard = self.env['almacenes.elite.signup'].create({'customer_type': 'empresa', 'employees': 60, 'accept': True})
        self.assertEqual(wizard.company_name, self.company.name, "the company data comes from this Odoo")
        self.assertEqual(wizard.vat, 'B46126314')
        action = wizard.action_signup()
        self.assertEqual(action['params']['type'], 'success')
        sent = self.api.last_params['alta']
        self.assertEqual((sent['tipo'], sent['empleados'], sent['nif'], sent['calle']), ('empresa', 60, 'B46126314', 'Calle Mayor, 1'))
        self.assertTrue(sent['odoo'].startswith('http'), "the address of this Odoo travels for «back to my Odoo»")
        self.assertTrue(self.company.ea_consent)
        self.assertEqual(self.company.ea_api_key, 'ea_' + 'd' * 64)
        self.assertEqual(self.company.ea_account_name, 'Cliente de prueba')
        self.assertEqual(self.company.ea_payment_mode, 'web')

    def test_signup_needs_the_conditions_and_existing_customers_get_their_key(self):
        self.company.write({'ea_api_key': False, 'ea_consent': False})
        wizard = self.env['almacenes.elite.signup'].create({'customer_type': 'autonomo', 'accept': False, 'company_name': 'Gestoría', 'vat': '00000000T'})
        with self.assertRaises(UserError):
            wizard.action_signup()
        wizard.accept = True
        action = wizard.action_signup()
        self.assertEqual(action['params']['type'], 'info')
        self.assertFalse(self.company.ea_api_key, "nothing changes: the office sends the key of the existing account")

    def _signup_existing(self, vat):
        self.company.write({'ea_api_key': False, 'ea_consent': False, 'ea_account_name': False, 'ea_partner_id': False,
                            'vat': 'ES' + vat, 'street': 'Calle Mayor, 1', 'zip': '46001', 'city': 'Valencia'})
        wizard = self.env['almacenes.elite.signup'].create({'customer_type': 'empresa', 'employees': 10, 'accept': True})
        return wizard.action_signup()

    def test_existing_customer_confirms_by_email_and_connects(self):
        action = self._signup_existing('B12345674')
        self.assertEqual(action['params']['type'], 'info')
        self.assertIn('c•••@cliente.test', action['params']['message'])
        self.assertFalse(self.company.ea_api_key, "no key until it is confirmed by email")
        self.assertEqual((self.company.ea_signup_token, self.company.ea_signup_sent_to), ('av_' + 'f' * 64, 'c•••@cliente.test'))
        self.assertTrue(self.company.ea_signup_date)
        self.assertEqual(self.company._ea_check_signup(), 'waiting')
        settings = self.env['res.config.settings'].create({})
        self.assertEqual(settings.action_ea_check_signup()['params']['type'], 'info', "not confirmed yet")
        self.api.enlace = 'lista'
        self.assertEqual(settings.action_ea_check_signup()['params']['type'], 'success')
        self.assertEqual(self.company.ea_api_key, 'ea_' + 'e' * 64)
        self.assertTrue(self.company.ea_consent)
        self.assertFalse(self.company.ea_signup_token or self.company.ea_signup_date or self.company.ea_signup_sent_to)
        self.assertEqual(self.company.ea_account_name, 'Cliente de prueba')
        self.assertEqual(self.company.ea_payment_mode, 'account', "an existing customer keeps its usual payment terms")
        self.assertTrue(self.company.ea_partner_id, "Almacenes Elite becomes the vendor as with a pasted key")

    def test_existing_customer_rejected_or_checked_by_almacenes_elite(self):
        self._signup_existing('B12345674')
        self.api.enlace = 'rechazada'
        self.assertEqual(self.company._ea_check_signup(), 'closed')
        self.assertFalse(self.company.ea_signup_token or self.company.ea_signup_date)
        self.assertIn('no se ha confirmado', self.company.ea_last_error)
        self.assertFalse(self.company.ea_api_key)
        # archived account or no email on file: Almacenes Elite checks it and this Odoo waits (no email address to show)
        self.api.enlace = 'revision'
        action = self._signup_existing('B87654323')
        self.assertEqual((action['params']['type'], action['params']['message']), ('info', 'Lo revisamos y te llamamos.'))
        self.assertEqual((self.company.ea_signup_token, self.company.ea_signup_sent_to), ('av_' + '9' * 64, False))
        self.assertEqual(self.company._ea_check_signup(), 'waiting')
        self.env['res.config.settings'].create({}).action_ea_cancel_signup()
        self.assertFalse(self.company.ea_signup_token or self.company.ea_signup_date)
