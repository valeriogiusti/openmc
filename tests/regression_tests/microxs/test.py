"""Test one-group cross section generation"""
from pathlib import Path

import numpy as np
import pytest
import openmc

from openmc.deplete import MicroXS

CHAIN_FILE = Path(__file__).parents[2] / "chain_simple.xml"

@pytest.fixture(scope="module")
def model():
    fuel = openmc.Material(name="uo2")
    fuel.add_element("U", 1, percent_type="ao", enrichment=4.25)
    fuel.add_element("O", 2)
    fuel.set_density("g/cc", 10.4)

    clad = openmc.Material(name="clad")
    clad.add_element("Zr", 1)
    clad.set_density("g/cc", 6)

    water = openmc.Material(name="water")
    water.add_element("O", 1)
    water.add_element("H", 2)
    water.set_density("g/cc", 1.0)
    water.add_s_alpha_beta("c_H_in_H2O")

    radii = [0.42, 0.45]
    fuel.volume = np.pi * radii[0] ** 2

    materials = openmc.Materials([fuel, clad, water])

    pin_surfaces = [openmc.ZCylinder(r=r) for r in radii]
    pin_univ = openmc.model.pin(pin_surfaces, materials)
    bound_box = openmc.rectangular_prism(1.24, 1.24, boundary_type="reflective")
    root_cell = openmc.Cell(fill=pin_univ, region=bound_box)
    geometry = openmc.Geometry([root_cell])

    settings = openmc.Settings()
    settings.particles = 1000
    settings.inactive = 10
    settings.batches = 50

    return openmc.Model(geometry, materials, settings)


def test_from_model(model):
    ref_xs = MicroXS.from_csv('test_reference.csv')
    test_xs = MicroXS.from_model(model, model.materials[0], CHAIN_FILE)

    np.testing.assert_allclose(test_xs, ref_xs, rtol=1e-11)
