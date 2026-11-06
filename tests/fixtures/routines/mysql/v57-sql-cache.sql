-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

CREATE DEFINER=`admin`@`%` TRIGGER `orders_ai` AFTER INSERT ON `orders` FOR EACH ROW SELECT SQL_CACHE COUNT(*) INTO @order_count FROM orders WHERE customer_id = NEW.customer_id
