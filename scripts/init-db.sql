CREATE DATABASE control_plane_db;

\connect telemetry_db
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS vector;

\connect control_plane_db
