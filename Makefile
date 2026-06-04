.PHONY: test

test:
	python3 -m unittest discover -v

.PHONY: single-file

single-file:
	python3 scripts/build_single_file.py
