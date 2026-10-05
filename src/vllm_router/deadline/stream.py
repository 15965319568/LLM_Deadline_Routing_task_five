"""Transport observation adapter for the pilot request monitor."""


class FirstToken:
    def __init__(self):
        self.found = False

    def feed(self, chunk):
        if self.found or not chunk:
            return False
        self.found = True
        return True
