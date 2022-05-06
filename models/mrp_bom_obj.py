from odoo import api,fields,models,_
class StockPickings(models.Model):
    _inherit = "mrp.bom"

    type = fields.Selection([
        ('normal', 'Manufacture this product'),
        ('phantom', 'Kit')], 'BoM Type',
        default='phantom', required=True)
    
    

