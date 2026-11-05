-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

/*!50003 SET @saved_cs_client      = @@character_set_client */ ;
/*!50003 SET character_set_client  = utf8mb4 */ ;
DELIMITER ;;
/*!50003 CREATE*/ /*!50017 DEFINER=`admin`@`%`*/ /*!50003 TRIGGER `lines_ai` AFTER INSERT ON `order_lines` FOR EACH ROW BEGIN
  UPDATE orders SET total = total + NEW.amount WHERE id = NEW.order_id;
END */;;
DELIMITER ;
/*!50003 SET character_set_client  = @saved_cs_client */ ;
