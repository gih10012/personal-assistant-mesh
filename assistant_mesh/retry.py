"""Small model-independent retry timing shared by local control services."""
import math


class BoundedBackoff:
    def __init__(self, initial, maximum=60):
        if (isinstance(initial, bool) or not isinstance(initial, (int, float))
                or not math.isfinite(initial) or not 0 < initial <= maximum <= 60):
            raise ValueError('channel_backoff_invalid')
        self.initial, self.maximum, self.value = initial, maximum, initial

    def failed(self):
        self.value = min(self.maximum, self.value * 2)
        return self.value

    def reset(self):
        self.value = self.initial
        return self.value
