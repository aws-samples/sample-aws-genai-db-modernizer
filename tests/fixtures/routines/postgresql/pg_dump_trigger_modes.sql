-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0
-- pg_dump 16.2 --schema-only --no-owner of a database with a trigger in each enable
-- mode (#2071), as pg_dump writes it: each CREATE TRIGGER, then its ALTER TABLE line.
--
-- PostgreSQL database dump
--

-- Dumped from database version 16.2
-- Dumped by pg_dump version 16.2

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: Sales; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA "Sales";


--
-- Name: fn(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.fn() RETURNS trigger
    LANGUAGE plpgsql
    AS $$BEGIN RETURN NULL; END$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: Order Lines; Type: TABLE; Schema: Sales; Owner: -
--

CREATE TABLE "Sales"."Order Lines" (
    id integer
);


--
-- Name: orders; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.orders (
    id integer NOT NULL
);


--
-- Name: orders orders_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_pkey PRIMARY KEY (id);


--
-- Name: Order Lines Line Audit; Type: TRIGGER; Schema: Sales; Owner: -
--

CREATE TRIGGER "Line Audit" AFTER INSERT ON "Sales"."Order Lines" FOR EACH ROW EXECUTE FUNCTION public.fn();

ALTER TABLE "Sales"."Order Lines" ENABLE ALWAYS TRIGGER "Line Audit";


--
-- Name: orders t_a; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER t_a AFTER DELETE ON public.orders FOR EACH ROW EXECUTE FUNCTION public.fn();

ALTER TABLE public.orders ENABLE ALWAYS TRIGGER t_a;


--
-- Name: orders t_d; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER t_d AFTER INSERT ON public.orders FOR EACH ROW EXECUTE FUNCTION public.fn();

ALTER TABLE public.orders DISABLE TRIGGER t_d;


--
-- Name: orders t_o; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER t_o AFTER INSERT ON public.orders FOR EACH ROW EXECUTE FUNCTION public.fn();


--
-- Name: orders t_r; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER t_r AFTER UPDATE ON public.orders FOR EACH ROW EXECUTE FUNCTION public.fn();

ALTER TABLE public.orders ENABLE REPLICA TRIGGER t_r;


--
-- PostgreSQL database dump complete
--
