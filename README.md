# Northline Goods - three-tier e-commerce app (CEA)

Web (Nginx) -> App (Flask API) -> Database (MySQL)

Features: product catalogue with search and categories, DB-backed shopping cart,
checkout with stock locking, order tracking, and an admin page (token protected).

## Run locally
    cp .env.example .env      # then edit the values
    docker compose up --build
    # open http://localhost   (admin page: http://localhost/#/admin)

## Run tests
    cd app && pip install -r requirements-dev.txt && pytest
