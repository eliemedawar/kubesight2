const express = require("express");
const { Client } = require("pg");
const { Kafka } = require("kafkajs");

const app = express();
const db = new Client({ connectionString: process.env.DATABASE_URL });
const kafka = new Kafka({ brokers: process.env.KAFKA_BOOTSTRAP_SERVERS.split(",") });

app.get("/payments/:id", async (request, response) => {
  const auth = await fetch(`${process.env.AUTH_SERVICE_URL}/authorize`);
  response.json({ id: request.params.id, authorized: auth.ok });
});

app.listen(8080);
