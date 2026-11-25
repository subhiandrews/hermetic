# Orders service: design note

The orders service turns a customer's cart into an order. It is one of the services of the Sock Shop,
an online shop that sells socks.

## Storage

Orders are kept in MongoDB, in the collection `orders` of the database at
`mongodb://orders-db:27017/data`. The service owns that database; no other service reads it.

## Other services

To place an order the service calls three other services over HTTP, in this order:

1. It asks the carts service for the customer's cart and the items in it.
2. It asks the user service for the customer's address and the card they have on file.
3. It asks the payment service to authorise the amount, and only creates the order if that succeeds.

When the order is placed it also tells the carts service to delete the cart.

## Not part of this note

Nothing is cached. The service does not call the internet.
