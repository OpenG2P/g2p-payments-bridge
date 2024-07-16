FROM bitnami/python:3.10.13-debian-11-r24

ARG container_user=openg2p
ARG container_user_group=openg2p
ARG container_user_uid=1001
ARG container_user_gid=1001

RUN groupadd -g ${container_user_gid} ${container_user_group} \
  && useradd -mN -u ${container_user_uid} -G ${container_user_group} -s /bin/bash ${container_user}

WORKDIR /app

RUN install_packages libpq-dev \
  && apt-get clean && rm -rf /var/lib/apt/lists /var/cache/apt/archives

RUN chown -R ${container_user}:${container_user_group} /app
USER ${container_user}

ADD --chown=${container_user}:${container_user_group} . /app/src
ADD --chown=${container_user}:${container_user_group} main.py /app

RUN python3 -m venv venv \
  && . ./venv/bin/activate
RUN python3 -m pip install \
  git+https://github.com/OpenG2P/openg2p-fastapi-common/@d2f2cadf01f561117fefea3245eb6e18dbdba826\#egg=openg2p-fastapi-common\&subdirectory=openg2p-fastapi-common \
  git+https://github.com/OpenG2P/openg2p-fastapi-common/@d2f2cadf01f561117fefea3245eb6e18dbdba826\#egg=openg2p-fastapi-auth\&subdirectory=openg2p-fastapi-auth \
  git+https://github.com/OpenG2P/openg2p-fastapi-common/@d2f2cadf01f561117fefea3245eb6e18dbdba826\#egg=openg2p-common-g2pconnect-id-mapper\&subdirectory=openg2p-common-g2pconnect-id-mapper \
  ./src/g2p-cash-transfer-bridge-core \
  ./src/g2p-cash-transfer-bridge-api \
  ./src/gctb-translate-id-fa

CMD python3 main.py migrate; \
  python3 main.py run
