-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

CREATE DEFINER=`app`@`localhost` TRIGGER `orders_bi` BEFORE INSERT ON `orders` FOR EACH ROW FOLLOWS `orders_bi_first` SET NEW.total = NEW.qty * NEW.price
