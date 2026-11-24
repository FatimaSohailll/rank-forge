"""Bundle textstat's pronunciation dictionary during build, not in a request."""
from pathlib import Path
import shutil
import tempfile

import nltk


def main():
    target = Path(__file__).resolve().parent / '.nltk_data'
    # NLTK requires an owner-private data path; build workspaces may be group-writable.
    with tempfile.TemporaryDirectory(prefix='rankforge-build-') as staging:
        if not nltk.download('cmudict', download_dir=staging, quiet=True, raise_on_error=True):
            raise RuntimeError('Could not bundle the SEO readability dictionary')
        nltk.data.find('corpora/cmudict', paths=[staging])
        shutil.copytree(Path(staging) / 'corpora' / 'cmudict', target / 'corpora' / 'cmudict', dirs_exist_ok=True)


if __name__ == '__main__':
    main()
