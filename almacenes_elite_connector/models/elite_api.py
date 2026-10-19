# -*- coding: utf-8 -*-
"""HTTP client for the Almacenes Elite customer API (one endpoint, operations selected with ``op``).

Contract (version 1): GET  /website/action/elite-api?op=<operation>&...   header ``Authorization: Bearer <key>``
                      POST /website/action/elite-api  (form: op, csrf_token, datos=<json>) after GET op=sesion
Every answer is JSON: ``{"ok": true, ...}`` or ``{"ok": false, "error": {"codigo", "mensaje"}}`` (messages in Spanish,
written for the person using Odoo). Orders are never sent from here: they are placed on almaceneselite.com (op ``entrar`` opens
the website already signed in) and come back with ``estado`` / ``pedidos``.
"""
import json
import logging

import requests

from odoo import _, api, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

API_PATH = '/website/action/elite-api'
DEFAULT_URL = 'https://almaceneselite.com'
TIMEOUT = 40
USER_AGENT = 'OdooAlmacenesEliteConnector/17.0.1.1.0'


class AlmacenesEliteApi(models.AbstractModel):
    _name = 'almacenes.elite.api'
    _description = 'Almacenes Elite API client'

    # ------------------------------------------------------------------ configuration
    @api.model
    def _ea_config(self, company=None, require_consent=True):
        company = (company or self.env.company).sudo()
        if require_consent and not company.ea_consent:
            raise UserError(_("The Almacenes Elite connection is disabled. Enable it in Purchase › Configuration › Settings."))
        if not company.ea_api_key:
            raise UserError(_("Add your Almacenes Elite connection key in Purchase › Configuration › Settings."))
        return (company.ea_api_url or DEFAULT_URL).rstrip('/'), company.ea_api_key.strip()

    # ------------------------------------------------------------------ low level
    @api.model
    def _ea_request(self, op, params=None, company=None, method='GET', data=None, session=None, raw=False, require_consent=True):
        """Returns (http_status, body). ``body`` is the decoded JSON (or bytes when ``raw``)."""
        url, key = self._ea_config(company, require_consent=require_consent)
        http = session or requests.Session()
        headers = {'Authorization': 'Bearer ' + key, 'Accept': 'application/json', 'User-Agent': USER_AGENT}
        try:
            if method == 'GET':
                resp = http.get(url + API_PATH, params=dict(params or {}, op=op), headers=headers, timeout=TIMEOUT)
            else:
                resp = http.post(url + API_PATH, data=dict(data or {}, op=op), headers=headers, timeout=TIMEOUT)
        except requests.exceptions.RequestException as exc:
            _logger.warning("Almacenes Elite API %s %s failed: %s", method, op, exc)
            raise UserError(_("Could not reach Almacenes Elite. Check your internet connection and try again.\n(%s)", exc)) from exc
        if raw and resp.status_code == 200 and not resp.headers.get('Content-Type', '').startswith('application/json'):
            return resp.status_code, resp.content
        try:
            body = resp.json()
        except ValueError:
            body = {'ok': False, 'error': {'codigo': 'http_%s' % resp.status_code,
                                           'mensaje': _("Unexpected answer from Almacenes Elite (HTTP %s).", resp.status_code)}}
        return resp.status_code, body

    @api.model
    def _ea_error_text(self, body):
        err = (body or {}).get('error') or {}
        return err.get('mensaje') or err.get('codigo') or _("Unknown error")

    @api.model
    def _ea_get(self, op, params=None, company=None, require_consent=True):
        """GET an operation and return the body; raises a readable UserError when it is not ok."""
        status, body = self._ea_request(op, params=params, company=company, require_consent=require_consent)
        if status != 200 or not body.get('ok'):
            raise UserError(_("Almacenes Elite: %s", self._ea_error_text(body)))
        return body

    @api.model
    def _ea_pdf(self, invoice_number, company=None):
        status, body = self._ea_request('factura_pdf', {'numero': invoice_number}, company=company, raw=True)
        return body if status == 200 and isinstance(body, bytes) else None

    # ------------------------------------------------------------------ POST (website session)
    @api.model
    def _ea_post(self, op, data, company=None):
        """POST an operation (form data) with the CSRF token of a fresh website session. Returns (status, body) without
        raising on business errors so the caller can show them."""
        http = requests.Session()
        status, body = self._ea_request('sesion', company=company, session=http)
        if status != 200 or not body.get('ok') or not body.get('csrf_token'):
            raise UserError(_("Almacenes Elite: %s", self._ea_error_text(body)))
        return self._ea_request(op, company=company, method='POST', session=http, data=dict(data, csrf_token=body['csrf_token']))

    @api.model
    def _ea_post_without_key(self, op, data, company=None):
        """The only calls made without a connection key (there is none yet): becoming a customer and collecting the key of an
        existing account once it is confirmed. Session + POST with its CSRF token. Returns (status, body)."""
        company = (company or self.env.company).sudo()
        url = (company.ea_api_url or DEFAULT_URL).rstrip('/')
        http = requests.Session()
        headers = {'Accept': 'application/json', 'User-Agent': USER_AGENT}
        try:
            resp = http.get(url + API_PATH, params={'op': 'sesion'}, headers=headers, timeout=TIMEOUT)
            token = resp.json().get('csrf_token') if resp.status_code == 200 else None
            if not token:
                raise UserError(_("Almacenes Elite is not available right now. Try again in a moment."))
            resp = http.post(url + API_PATH, data=dict(data, op=op, csrf_token=token), headers=headers, timeout=TIMEOUT)
        except (requests.exceptions.RequestException, ValueError) as exc:
            raise UserError(_("Could not reach Almacenes Elite. Check your internet connection and try again.\n(%s)", exc)) from exc
        try:
            body = resp.json()
        except ValueError:
            body = {'ok': False, 'error': {'codigo': 'http_%s' % resp.status_code,
                                           'mensaje': _("Unexpected answer from Almacenes Elite (HTTP %s).", resp.status_code)}}
        return resp.status_code, body

    @api.model
    def _ea_signup(self, data, company=None):
        """Becoming a customer from Odoo. Returns (status, body): a new customer gets the connection key at once (shown only this
        once); an existing customer (same tax ID) gets ``estado`` «confirmar» or «revision» and a one-time token to collect the
        key when it is confirmed by email (or approved by Almacenes Elite)."""
        return self._ea_post_without_key('alta', {'datos': json.dumps(data)}, company=company)

    @api.model
    def _ea_signup_status(self, token, company=None):
        """Existing customer: has the connection been confirmed? ``estado`` pendiente / revision / lista (with the key, once)."""
        return self._ea_post_without_key('alta_estado', {'vale': token}, company=company)

