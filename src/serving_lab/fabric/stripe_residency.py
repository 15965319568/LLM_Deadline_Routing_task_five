"""Upload accounting adapter for the original full-page allocator."""
def workspace_pages(pages,uploads):
    return max(len(pages),len(uploads.buffers))

def published_pages(cache):
    return len(cache)
