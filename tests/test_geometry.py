import pytest

from vlc_monitor.ui.geometry import (
    content_rect, move_rect, normalized_from_points, pixel_bounds,
    resize_rect, to_display, to_normalized, valid_size,
)


def test_black_bars_and_logical_pixels():
    content = content_rect(1000, 1000, 16 / 9)
    assert content == (0, 218.75, 1000, 562.5)
    assert to_normalized((500, 100), content) is None
    assert to_normalized((500, 500), content) == (.5, .5)
    assert to_normalized((500, 0), content, clamp=True) == (.5, 0)
    # Retina/150% DPI does not multiply Qt logical mouse coordinates a second time.
    assert to_display((.1, .2, .4, .3), content) == pytest.approx((100, 331.25, 400, 168.75))
    pillarbox = content_rect(1200, 600, 1)
    assert pillarbox == (300, 0, 600, 600)
    assert to_normalized((299, 300), pillarbox) is None


def test_move_resize_and_reverse_draw():
    assert normalized_from_points((.8, .9), (.2, .3)) == pytest.approx((.2, .3, .6, .6))
    assert move_rect((.1, .2, .4, .3), -2, 2) == pytest.approx((0, .7, .4, .3))
    assert resize_rect((.1, .2, .4, .3), (2, 2), 1920, 1080) == pytest.approx((.1, .2, .9, .8))
    resized = resize_rect((.1, .2, .4, .3), (0, 0), 1920, 1080)
    assert valid_size(resized, 1920, 1080)
    assert not valid_size((0, 0, .001, .001), 1920, 1080)


def test_resolution_mapping_and_nonempty_clamped_crop():
    rect = (.25, .25, .5, .5)
    assert pixel_bounds(rect, 1920, 1080) == (480, 270, 1440, 810)
    assert pixel_bounds(rect, 1280, 720) == (320, 180, 960, 540)
    assert pixel_bounds((-.1, -.1, .3, .3), 100, 100) == (0, 0, 20, 20)
    assert pixel_bounds((.99, .99, .1, .1), 100, 100) == (99, 99, 100, 100)
    with pytest.raises(ValueError):
        pixel_bounds((2, 2, .1, .1), 100, 100)
    assert content_rect(0, 600, 1) == (0, 0, 0, 0)
    assert to_normalized((1, 1), (0, 0, 0, 0)) is None
