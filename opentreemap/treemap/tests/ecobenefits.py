# -*- coding: utf-8 -*-
from __future__ import print_function
from __future__ import unicode_literals
from __future__ import division

import json

from treemap.models import Plot, Tree, Species, ITreeRegion
from treemap.tests import (UrlTestCase, make_instance, make_commander_user,
                           make_request)

from treemap import ecobackend
from treemap.ecobenefits import (TreeBenefitsCalculator,
                                 _combine_benefit_basis,
                                 _annotate_basis_with_extra_stats,
                                 _combine_grouped_benefits)

from treemap.views import search_tree_benefits


class EcoTest(UrlTestCase):
    def setUp(self):
        # Example url for
        # CEAT, 1630 dbh, NoEastXXX
        # eco.json?otmcode=CEAT&diameter=1630&region=NoEastXXX
        def mockbenefits(*args, **kwargs):
            benefits = {
                "Benefits": {
                    "aq_nox_avoided": 0.6792,
                    "aq_nox_dep": 0.371,
                    "aq_ozone_dep": 0.775,
                    "aq_pm10_avoided": 0.0436,
                    "aq_pm10_dep": 0.491,
                    "aq_sox_avoided": 0.372,
                    "aq_sox_dep": 0.21,
                    "aq_voc_avoided": 0.0254,
                    "bvoc": -0.077,
                    "co2_avoided": 255.5,
                    "co2_sequestered": 0,
                    "co2_storage": 6575,
                    "electricity": 187,
                    "hydro_interception": 12.06,
                    "natural_gas": 5834.1
                }
            }
            return (benefits, None)

        region = ITreeRegion.objects.get(code='NoEastXXX')
        p = region.geometry.point_on_surface

        self.instance = make_instance(is_public=True, point=p)
        self.user = make_commander_user(self.instance)

        self.species = Species(otm_code='CEAT',
                               genus='cedrus',
                               species='atlantica',
                               max_dbh=2000,
                               max_height=100,
                               instance=self.instance)

        self.species.save_with_user(self.user)

        self.plot = Plot(geom=p, instance=self.instance)

        self.plot.save_with_user(self.user)

        self.tree = Tree(plot=self.plot,
                         instance=self.instance,
                         readonly=False,
                         species=self.species,
                         diameter=1630)

        self.tree.save_with_user(self.user)

        self.origBenefitFn = ecobackend.json_benefits_call
        ecobackend.json_benefits_call = mockbenefits

    def tearDown(self):
        ecobackend.json_benefits_call = self.origBenefitFn

    def assert_benefit_value(self, bens, benefit, unit, value):
        self.assertEqual(bens[benefit]['unit'], unit)
        self.assertEqual(int(float(bens[benefit]['value'])), value)

    def test_eco_benefit_sanity(self):
        rslt, basis, error = TreeBenefitsCalculator()\
            .benefits_for_object(self.instance, self.tree.plot)

        bens = rslt['plot']

        self.assert_benefit_value(bens, 'energy', 'kwh', 1896)
        self.assert_benefit_value(bens, 'airquality', 'lbs/year', 6)
        self.assert_benefit_value(bens, 'stormwater', 'gal', 3185)
        self.assert_benefit_value(bens, 'co2', 'lbs/year', 563)

    def testSearchBenefits(self):
        request = make_request(
            {'q': json.dumps({'tree.readonly': {'IS': False}})})  # all trees
        request.instance_supports_ecobenefits = self.instance\
                                                    .has_itree_region()
        result = search_tree_benefits(request, self.instance)

        benefits = result['benefits']

        self.assertTrue(len(benefits) > 0)

    def test_group_basis_empty(self):
        basis = {}
        example = {
            'group1': {
                'n_objects_used': 5,
                'n_objects_discarded': 8
            },
            'group2': {
                'n_objects_used': 10,
                'n_objects_discarded': 12
            }
        }

        _combine_benefit_basis(basis, example)
        self.assertEqual(basis, example)

    def test_group_basis_combine_new_group(self):
        # New groups are added
        basis = {
            'group1': {
                'n_objects_used': 5,
                'n_objects_discarded': 8
            }
        }
        new_group = {
            'group2': {
                'n_objects_used': 13,
                'n_objects_discarded': 4
            }
        }
        target = {
            'group1': {
                'n_objects_used': 5,
                'n_objects_discarded': 8
            },
            'group2': {
                'n_objects_used': 13,
                'n_objects_discarded': 4
            }
        }
        _combine_benefit_basis(basis, new_group)
        self.assertEqual(basis, target)

    def test_group_basis_combine_existing_groups(self):
        basis = {
            'group1': {
                'n_objects_used': 5,
                'n_objects_discarded': 8
            }
        }
        update_group = {
            'group1': {
                'n_objects_used': 13,
                'n_objects_discarded': 4
            }
        }
        target = {
            'group1': {
                'n_objects_used': 18,
                'n_objects_discarded': 12
            }
        }
        _combine_benefit_basis(basis, update_group)
        self.assertEqual(basis, target)

    def test_combine_benefit_groups_empty(self):
        # with and without currency
        base_group = {'group1':
                      {'benefit1':
                       {'value': 3,
                        'currency': 9,
                        'unit': 'gal',
                        'label': 'stormwater',
                        'unit-name': 'eco'},
                       'benefit2':
                       {'value': 3,
                        'currency': 9,
                        'unit': 'gal',
                        'label': 'stormwater',
                        'unit-name': 'eco'}}}
        groups = {}
        _combine_grouped_benefits(groups, base_group)

        self.assertEqual(groups, base_group)

    def test_combine_benefit_groups_no_overlap(self):
        base_group = {'group1':
                      {'benefit1':
                       {'value': 3,
                        'currency': 9,
                        'unit': 'gal',
                        'label': 'stormwater',
                        'unit-name': 'eco'},
                       'benefit2':
                       {'value': 4,
                        'currency': 10,
                        'unit': 'gal',
                        'label': 'stormwater',
                        'unit-name': 'eco'}}}
        new_group = {'group2':
                     {'benefit1':
                      {'value': 5,
                       'currency': 11,
                       'unit': 'gal',
                       'label': 'stormwater',
                       'unit-name': 'eco'},
                      'benefit2':
                      {'value': 6,
                       'currency': 19,
                       'unit': 'gal',
                       'label': 'stormwater',
                       'unit-name': 'eco'}}}
        groups = {}
        _combine_grouped_benefits(groups, base_group)
        _combine_grouped_benefits(groups, new_group)

        target = {'group1': base_group['group1'],
                  'group2': new_group['group2']}

        self.assertEqual(groups, target)

    def test_combine_benefit_groups_sums_benefits(self):
        base_group = {'group1':
                      {'benefit1':
                       {'value': 3,
                        'unit': 'gal',
                        'label': 'stormwater',
                        'unit-name': 'eco'},
                       'benefit2':
                       {'value': 4,
                        'currency': 10,
                        'unit': 'gal',
                        'label': 'stormwater',
                        'unit-name': 'eco'},
                       'benefit3':
                       {'value': 32,
                        'currency': 919,
                        'unit': 'gal',
                        'label': 'stormwater',
                        'unit-name': 'eco'}}}
        new_group = {'group1':
                     {'benefit1':
                      {'value': 5,
                       'currency': 11,
                       'unit': 'gal',
                       'label': 'stormwater',
                       'unit-name': 'eco'},
                      'benefit2':
                      {'value': 7,
                       'unit': 'gal',
                       'currency': 19,
                       'label': 'stormwater',
                       'unit-name': 'eco'},
                      'benefit4':
                      {'value': 7,
                       'unit': 'gal',
                       'label': 'stormwater',
                       'unit-name': 'eco'}}}
        groups = {}
        _combine_grouped_benefits(groups, base_group)
        _combine_grouped_benefits(groups, new_group)

        target = {'group1':
                  {'benefit1':
                   {'value': 8,
                    'currency': 11,
                    'unit': 'gal',
                    'label': 'stormwater',
                    'unit-name': 'eco'},
                   'benefit2':
                   {'value': 11,
                    'currency': 29,
                    'unit': 'gal',
                    'label': 'stormwater',
                    'unit-name': 'eco'},
                   'benefit3':
                   {'value': 32,
                    'currency': 919,
                    'unit': 'gal',
                    'label': 'stormwater',
                    'unit-name': 'eco'},
                   'benefit4':
                   {'value': 7,
                    'unit': 'gal',
                    'label': 'stormwater',
                    'unit-name': 'eco'}}}

        self.assertEqual(groups, target)

    def test_annotates_basis(self):
        basis = {
            'group1': {
                'n_objects_used': 5,
                'n_objects_discarded': 15
            },
            'group2': {
                'n_objects_used': 2,
                'n_objects_discarded': 18
            }
        }
        target = {
            'group1': {
                'n_objects_used': 5,
                'n_objects_discarded': 15,
                'n_total': 20,
                'n_pct_calculated': 0.25
            },
            'group2': {
                'n_objects_used': 2,
                'n_objects_discarded': 18,
                'n_total': 20,
                'n_pct_calculated': 0.1
            }
        }
        _annotate_basis_with_extra_stats(basis)

        self.assertEqual(basis, target)
