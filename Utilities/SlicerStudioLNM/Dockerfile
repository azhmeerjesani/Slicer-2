# slicer-lnm: Slicer Studio's lesion network mapping library as a BIDS-App container image.
#
# Local builds only: the image is never pushed to a registry. The build context is the module folder,
# so that only the library is sent to the builder (see README.md next to this file):
#
#   cd Modules/Scripted/LesionNetworkMapping
#   docker build -f ../../../Utilities/SlicerStudioLNM/Dockerfile \
#     --build-arg SLICER_FORK_REV=$(git rev-parse --short=12 HEAD) -t slicer-lnm:local .
#
# Both architectures (linux/amd64 and linux/arm64; numpy and scipy publish wheels for both), saved to a
# local file instead of a registry:
#
#   docker buildx build --platform linux/amd64,linux/arm64 -f ../../../Utilities/SlicerStudioLNM/Dockerfile \
#     --build-arg SLICER_FORK_REV=$(git rev-parse --short=12 HEAD) --output type=oci,dest=slicer-lnm.oci.tar .
#
# The image holds no data: no connectome, template, atlas or lesion. The connectome is bind-mounted
# read-only at run time, for example:
#
#   docker run --rm -v /data/bids:/bids:ro -v /data/out:/out -v /data/GSP1000.lnmc:/connectome:ro \
#     slicer-lnm:local /bids /out participant --connectome /connectome --accept-terms --threads 4

ARG PYTHON_IMAGE=python:3.12-slim

# Wheel of the library (pyproject.toml, setuptools from PyPI in the isolated build environment)
FROM ${PYTHON_IMAGE} AS wheel
ARG SLICER_FORK_REV=local
WORKDIR /src
COPY pyproject.toml ./
COPY LesionNetworkMappingLib ./LesionNetworkMappingLib
RUN find LesionNetworkMappingLib -name "__pycache__" -type d -prune -exec rm -rf {} + \
  && SLICER_FORK_REV="${SLICER_FORK_REV}" pip wheel --no-cache-dir --no-deps -w /dist .

FROM ${PYTHON_IMAGE}
ARG SLICER_FORK_REV=local
# numpy and scipy at the versions that Slicer Studio bundles (PythonSlicer), so that a container run
# and an application run use the same numerics. Pillow (the version Slicer bundles) renders report.html and
# the figures; --build-arg WITH_PILLOW=0 leaves it out (the report stage then fails; the data outputs are complete).
ARG NUMPY_VERSION=2.4.6
ARG SCIPY_VERSION=1.17.1
ARG WITH_PILLOW=1
ARG PILLOW_VERSION=12.2.0
LABEL org.opencontainers.image.title="slicer-lnm" \
  org.opencontainers.image.description="Lesion network mapping BIDS-App (Slicer Studio LesionNetworkMappingLib). Research use only. Contains no data." \
  org.opencontainers.image.licenses="LicenseRef-3D-Slicer" \
  org.opencontainers.image.revision="${SLICER_FORK_REV}"
ENV PYTHONDONTWRITEBYTECODE=1 \
  PYTHONUNBUFFERED=1 \
  PIP_DISABLE_PIP_VERSION_CHECK=1 \
  SLICER_LNM_OFFLINE=1
RUN pip install --no-cache-dir "numpy==${NUMPY_VERSION}" "scipy==${SCIPY_VERSION}" \
  && if [ "${WITH_PILLOW}" = "1" ]; then pip install --no-cache-dir "pillow==${PILLOW_VERSION}"; fi
COPY --from=wheel /dist/ /tmp/dist/
RUN pip install --no-cache-dir --no-deps /tmp/dist/slicer_lnm-*.whl && rm -rf /tmp/dist \
  && useradd --create-home --uid 1000 lnm \
  && slicer-lnm --version
USER lnm
WORKDIR /home/lnm
ENTRYPOINT ["slicer-lnm"]
CMD ["--help"]
