from odoo import models, fields, api, _
import mysql.connector as mysql
import logging
import datetime
import requests
from dateutil.relativedelta import relativedelta

_logger = logging.getLogger(__name__)

class Connections(models.Model):
    _name = 'mybizna.isp.connections'
    _rec_name = 'username'

    package_id = fields.Many2one('mybizna.isp.packages', string='Package')
    partner_id = fields.Many2one('res.partner', 'Partner', ondelete="cascade")
    invoice_id = fields.Many2one('account.move', string='Invoice')
    username = fields.Char('Username', required=True)
    password = fields.Char('Password', required=True)
    expiry_date = fields.Date('Expiry Date')
    billing_date = fields.Date('Billing Date')
    params = fields.Text('Params Text')
    is_setup = fields.Boolean('Is Setup', default=False)
    is_paid = fields.Boolean('Is Paid', default=False)
    status = fields.Selection(
        [('new', 'New'), ('active', 'Active'), ('inactive', 'In Active'), ('closed', 'Closed')],
        'Status', required=True, default='new')

    connections_setupitems_ids = fields.One2many(
        'mybizna.isp.connections_setupitems', 'connection_id', 'Setup Items', track_visibility='onchange')
    connections_invoices_ids = fields.One2many(
        'mybizna.isp.connections_invoices', 'connection_id', 'Invoices', track_visibility='onchange')

    @api.model
    def create(self, values):
        res = super().create(values)

        items = self.env['mybizna.isp.packages_setupitems'].search([
            ("package_id", "=", res.package_id.id),
            ("published", "=", 1),
        ])

        for item in items:
            setup_item_data = {
                'title': item.title,
                'description': item.description,
                'currency_id': item.currency_id.id,
                'connection_id': res.id,
                'amount': item.amount,
            }
            self.env['mybizna.isp.connections_setupitems'].create(setup_item_data)
            _logger.info(f"Setup item created for connection {res.id} with title: {item.title}")

        return res

    def generate_invoice(self):
        invoice_line_ids = []

        items = self.env['mybizna.isp.connections_setupitems'].search([
            ("connection_id", "=", self.id)
        ])

        if not items:
            items = self.env['mybizna.isp.packages_setupitems'].search([
                ("package_id", "=", self.package_id.id),
                ("published", "=", 1),
            ])
            for item in items:
                setup_item_data = {
                    'title': item.title,
                    'description': item.description,
                    'currency_id': item.currency_id.id,
                    'connection_id': self.id,
                    'amount': item.amount,
                }
                self.env['mybizna.isp.connections_setupitems'].create(setup_item_data)

        for item in items:
            invoice_line_ids.append((0, 0, {
                'name': item.title,
                'quantity': 1,
                'price_unit': item.amount,
                'account_id': 21,  # Replace with appropriate account ID
            }))

        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner_id.id,
            'user_id': self.env.user.id,
            'invoice_line_ids': invoice_line_ids,
        })
        invoice.action_post()
        self.reconcile_invoice(invoice)

        self.env['mybizna.isp.connections_invoices'].create({
            'connection_id': self.id,
            'invoice_id': invoice.id
        })

        return self.write({'is_setup': True, 'invoice_id': invoice.id})

    def reconcile_invoice(self, invoice):
        if invoice.state != 'posted' or invoice.payment_state not in ('not_paid', 'partial') or not invoice.is_invoice(include_receipts=True):
            return False

        pay_term_lines = invoice.line_ids.filtered(
            lambda line: line.account_id.user_type_id.type in ('receivable', 'payable')
        )

        domain = [
            ('account_id', 'in', pay_term_lines.account_id.ids),
            ('move_id.state', '=', 'posted'),
            ('partner_id', '=', invoice.commercial_partner_id.id),
            ('reconciled', '=', False),
            '|', ('amount_residual', '!=', 0.0), ('amount_residual_currency', '!=', 0.0),
        ]

        if invoice.is_inbound():
            domain.append(('balance', '<', 0.0))
        else:
            domain.append(('balance', '>', 0.0))

        for line in self.env['account.move.line'].search(domain):
            lines = line | invoice.line_ids.filtered(
                lambda line: line.account_id == line.account_id and not line.reconciled
            )
            lines.reconcile()

    def addToRadius(self, connection):
        speed = connection.package_id.speed + connection.package_id.speed_type

        actions = [
            f"DELETE FROM radcheck WHERE username='{connection.username}' and attribute='Cleartext-Password'",
            f'INSERT INTO radcheck (username, attribute, op, value) VALUES ("{connection.username}", "Cleartext-Password", ":=", "{connection.password}");',
            f"DELETE FROM radcheck WHERE username='{connection.username}' and attribute='User-Profile'",
            f'INSERT INTO radcheck (username, attribute, op, value) VALUES ("{connection.username}", "User-Profile", ":=", "{speed}_Profile");',
        ]

        try:
            if connection.gateway.by_sql_file:
                for action in actions:
                    response = requests.post(
                        f'http://{connection.package_id.gateway_id.ip_address}/isp/query.php',
                        data={'query': action}
                    )
                    _logger.info(f"Radius action response: {response.content}")
            else:
                with mysql.connect(
                    host=connection.package_id.gateway_id.ip_address,
                    user=connection.package_id.gateway_id.username,
                    passwd=connection.package_id.gateway_id.password,
                    database=connection.package_id.gateway_id.database
                ) as db:
                    with db.cursor() as cursor:
                        for action in actions:
                            cursor.execute(action)
                            db.commit()
                            _logger.info(f"Executed action: {action}")

        except mysql.Error as err:
            _logger.error(f"Error adding to radius for connection {connection.id}: {err}")
        except Exception as e:
            _logger.error(f"General error adding to radius for connection {connection.id}: {e}")

    def processExpiry(self):
        connections = self.env['mybizna.isp.connections'].search([
            ("status", "=", 'active'),
            ("is_paid", "=", True),
            ('expiry_date', '<=', datetime.date.today())
        ])

        packages = self.env['mybizna.isp.packages'].search([], order="amount asc")

        for connection in connections:
            connection.write({
                'is_paid': False,
                'package_id': packages[0].id,
            })
            self.env.cr.commit()
            connection.addToRadius(connection)

    def prepareBilling(self):
        gap_days = 5
        connections = self.env['mybizna.isp.connections'].search([
            ("status", "=", 'active'),
            ('billing_date', '<=', (datetime.date.today() + relativedelta(days=gap_days)))
        ])

        for connection in connections:
            kwargs = self.getDateKwargs(connection)

            curr_billing_date = connection.billing_date or datetime.date.today()
            start_date = curr_billing_date.strftime('%Y-%m-%d')
            end_date = (curr_billing_date + relativedelta(**kwargs)).strftime('%Y-%m-%d')

            connection.write({'billing_date': end_date})
            self.env.cr.commit()

            billing = self.env['mybizna.isp.billing'].create({
                'connection_id': connection.id,
                'title': connection.package_id.title,
                'description': connection.package_id.title,
                'start_date': start_date,
                'end_date': end_date,
            })

            self.env['mybizna.isp.billing_items'].create({
                'title': connection.package_id.title,
                'description': connection.package_id.title,
                'billing_id': billing.id,
                'amount': connection.package_id.amount,
            })

            self.env['mybizna.isp.billing'].generate_invoice(billing)

    def getDateKwargs(self, connection):
        duration_type = connection.package_id.billing_cycle_id.duration_type
        duration = connection.package_id.billing_cycle_id.duration
        kwargs = {duration_type: duration} if duration_type in ["days", "weeks", "months"] else {"months": 1}
        return kwargs

    def processNewConnections(self):
        connections = self.env['mybizna.isp.connections'].search([
            ("status", "=", 'new'),
            ('invoice_id.payment_state', '=', 'paid')
        ])

        for connection in connections:
            kwargs = self.getDateKwargs(connection)
            billing_date = (datetime.date.today() + relativedelta(**kwargs)).strftime('%Y-%m-%d')

            connection.write({
                'is_paid': True,
                'status': 'active',
                'billing_date': billing_date,
            })
            self.env.cr.commit()
            connection.addToRadius(connection)

    def processAllConnections(self):
        connections = self.env['mybizna.isp.connections'].search([
            ("status", "=", 'active'),
            ('invoice_id.payment_state', '=', 'paid')
        ])

        for connection in connections:
            connection.addToRadius(connection)
