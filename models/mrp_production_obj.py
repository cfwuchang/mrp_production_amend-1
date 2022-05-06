import json
import datetime
import math
import operator as py_operator
import re

from collections import defaultdict
from dateutil.relativedelta import relativedelta
from itertools import groupby

from odoo.exceptions import AccessError, UserError
from odoo.tools import float_compare, float_round, float_is_zero, format_datetime
from odoo.tools.misc import format_date

from odoo.addons.stock.models.stock_move import PROCUREMENT_PRIORITIES
from odoo import api,fields,models,_
class StockPickings(models.Model):
    _inherit = "mrp.production"


    def button_mark_done(self):
        if self.user_id.id != self.env.user.id:
            raise UserError('你不能操作他人的订单0')
        else:
            self._button_mark_done_sanity_checks()

            if not self.env.context.get('button_mark_done_production_ids'):
                self = self.with_context(button_mark_done_production_ids=self.ids)
            res = self._pre_button_mark_done()
            if res is not True:
                return res

            if self.env.context.get('mo_ids_to_backorder'):
                productions_to_backorder = self.browse(self.env.context['mo_ids_to_backorder'])
                productions_not_to_backorder = self - productions_to_backorder
            else:
                productions_not_to_backorder = self
                productions_to_backorder = self.env['mrp.production']

            self.workorder_ids.button_finish()

            productions_not_to_backorder._post_inventory(cancel_backorder=True)
            productions_to_backorder._post_inventory(cancel_backorder=False)
            backorders = productions_to_backorder._generate_backorder_productions()

            # if completed products make other confirmed/partially_available moves available, assign them
            done_move_finished_ids = (productions_to_backorder.move_finished_ids | productions_not_to_backorder.move_finished_ids).filtered(lambda m: m.state != 'done')
            done_move_finished_ids._trigger_assign()

            # Moves without quantity done are not posted => set them as done instead of canceling. In
            # case the user edits the MO later on and sets some consumed quantity on those, we do not
            # want the move lines to be canceled.
            (productions_not_to_backorder.move_raw_ids | productions_not_to_backorder.move_finished_ids).filtered(lambda x: x.state not in ('done', 'cancel')).write({
                'state': 'done',
                'product_uom_qty': 0.0,
            })

            for production in self:
                production.write({
                    'date_finished': fields.Datetime.now(),
                    'product_qty': production.qty_produced,
                    'priority': '0',
                    'is_locked': True,
                })

            for workorder in self.workorder_ids.filtered(lambda w: w.state not in ('done', 'cancel')):
                workorder.duration_expected = workorder._get_duration_expected()

            if not backorders:
                if self.env.context.get('from_workorder'):
                    return {
                        'type': 'ir.actions.act_window',
                        'res_model': 'mrp.production',
                        'views': [[self.env.ref('mrp.mrp_production_form_view').id, 'form']],
                        'res_id': self.id,
                        'target': 'main',
                    }
                return True
            context = self.env.context.copy()
            context = {k: v for k, v in context.items() if not k.startswith('default_')}
            for k, v in context.items():
                if k.startswith('skip_'):
                    context[k] = False
            action = {
                'res_model': 'mrp.production',
                'type': 'ir.actions.act_window',
                'context': dict(context, mo_ids_to_backorder=None, button_mark_done_production_ids=None)
            }
            if len(backorders) != 1:
                action.update({
                    'view_mode': 'form',
                    'res_id': backorders[0].id,
                })
            else:
                action.update({
                    'name': _("Backorder MO"),
                    'domain': [('id', 'in', backorders.ids)],
                    'view_mode': 'tree,form',
                })
            return action

    
    # def action_confirm(self):
    #     if self.user_id.id != self.env.user.id:
    #         raise UserError('你不能操作他人的订单1')
    #     else:
    #         self._check_company()
    #         for production in self:
    #             if production.bom_id:
    #                 production.consumption = production.bom_id.consumption
    #             if not production.move_raw_ids:
    #                 raise UserError(_("Add some materials to consume before marking this MO as to do."))
    #             # In case of Serial number tracking, force the UoM to the UoM of product
    #             if production.product_tracking != 'serial' and production.product_uom_id != production.product_id.uom_id:
    #                 production.write({
    #                     'product_qty': production.product_uom_id._compute_quantity(production.product_qty, production.product_id.uom_id),
    #                     'product_uom_id': production.product_id.uom_id
    #                 })
    #                 for move_finish in production.move_finished_ids.filtered(lambda m: m.product_id != production.product_id):
    #                     move_finish.write({
    #                         'product_uom_qty': move_finish.product_uom._compute_quantity(move_finish.product_uom_qty, move_finish.product_id.uom_id),
    #                         'product_uom': move_finish.product_id.uom_id
    #                     })
    #             production.move_raw_ids._adjust_procure_method()
    #             (production.move_raw_ids | production.move_finished_ids)._action_confirm()
    #             production.workorder_ids._action_confirm()

    #         # run scheduler for moves forecasted to not have enough in stock
    #         self.move_raw_ids._trigger_scheduler()
    #         return True 

    
    def button_plan(self):
        if self.user_id.id != self.env.user.id:
            raise UserError('你不能操作他人的订单2')
        else:
            """ Create work orders. And probably do stuff, like things. """
            orders_to_plan = self.filtered(lambda order: not order.is_planned)
            orders_to_confirm = orders_to_plan.filtered(lambda mo: mo.state != 'draft')
            orders_to_confirm.action_confirm()
            for order in orders_to_plan:
                order._plan_workorders()
            return True

    
    def button_unplan(self):
        if self.user_id.id != self.env.user.id:
            raise UserError('你不能操作他人的订单3')
        else:
            if any(wo.state != 'done' for wo in self.workorder_ids):
                raise UserError(_("Some work orders are already done, you cannot unplan this manufacturing order."))
            elif any(wo.state != 'progress' for wo in self.workorder_ids):
                raise UserError(_("Some work orders have already started, you cannot unplan this manufacturing order."))

            self.workorder_ids.leave_id.unlink()
            self.workorder_ids.write({
                'date_planned_start': False,
                'date_planned_finished': False,
            })

    
    def button_unreserve(self):
        if self.user_id.id != self.env.user.id:
            raise UserError('你不能操作他人的订单5')
        else:
            self.ensure_one()
            self.do_unreserve()
            return True
    
    
    def button_scrap(self):
        if self.user_id.id != self.env.user.id:
            raise UserError('你不能操作他人的订单6')
        else:
            self.ensure_one()
            return {
                'name': _('Scrap'),
                'view_mode': 'form',
                'res_model': 'stock.scrap',
                'view_id': self.env.ref('stock.stock_scrap_form_view2').id,
                'type': 'ir.actions.act_window',
                'context': {'default_production_id': self.id,
                            'product_ids': (self.move_raw_ids.filtered(lambda x: x.state not in ('done', 'cancel')) | self.move_finished_ids.filtered(lambda x: x.state != 'done')).mapped('product_id').ids,
                            'default_company_id': self.company_id.id
                            },
                'target': 'new',
            }
    
    
    def action_cancel(self):
        if self.user_id.id != self.env.user.id:
            raise UserError('你不能操作他人的订单7')
        else:
            """ Cancels production order, unfinished stock moves and set procurement
            orders in exception """
            if not self.move_raw_ids:
                self.state = 'cancel'
                return True
            self._action_cancel()
            return True

    
    def button_unbuild(self):
        if self.user_id.id != self.env.user.id:
            raise UserError('你不能操作他人的订单8')
        else:
            self.ensure_one()
            return {
                'name': _('Unbuild: %s', self.product_id.display_name),
                'view_mode': 'form',
                'res_model': 'mrp.unbuild',
                'view_id': self.env.ref('mrp.mrp_unbuild_form_view_simplified').id,
                'type': 'ir.actions.act_window',
                'context': {'default_product_id': self.product_id.id,
                            'default_mo_id': self.id,
                            'default_company_id': self.company_id.id,
                            'default_location_id': self.location_dest_id.id,
                            'default_location_dest_id': self.location_src_id.id,
                            'create': False, 'edit': False},
                'target': 'new',
            }


