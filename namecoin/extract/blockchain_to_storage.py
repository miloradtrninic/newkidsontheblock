#!/usr/bin/env python
from jsonrpc import ServiceProxy
import sys
import csv
import argparse
import ConfigParser
import logging
import os
import pika
import json
import pickle
from collections import OrderedDict

# Initialize argument parser
parser = argparse.ArgumentParser(description="Pull blockchain data from AMQP and write to storage (DB or CSV).")

# A config file is needed.
parser.add_argument("-c", "--config", action="store", help="config file name", default="namecoin_extractor.conf")

# Debug output - this will be written to log file
parser.add_argument("-d", "--debug", action="store_true", help="Enable debug output")

# Enable DB dry-run -- do not send to DB, but print out to stdout
parser.add_argument("--dryrun", action="store_true", help="Do dry run for DB, i.e. print to stdout instead of sending to DB.")

# Load from directory with pickles, not AMQP
parser.add_argument("--loadpickles", action="store", help="Load from directory with pickles, not AMQP. Requires [path] (default: .)")

# Go through options passed.
args = parser.parse_args()

# set up dry run
dry_run = True if args.dryrun else False
if not dry_run:
    import psycopg2

# Fetch a config file name, if given
config_fn = args.config if args.config else "namecoin_extractor.conf"

# Now parse config file options
scp = ConfigParser.SafeConfigParser()
scp.read(config_fn)

config_read_fail = False
for section in ["logging", "db"]:
    if not scp.has_section(section):
        print("Missing section %s in config file." % section)
        config_read_fail = True

if not scp.has_section("amqp") and not args.loadpickles:
    print("Missing section amqp in config file, but AMQP loading requested." % section)
    config_read_fail = True

# Set up log
log_file = scp.get("logging", "log_file") if scp.has_option("logging", "log_file") and scp.get("logging", "log_file") != "" else "blockchain_to_storage.log"
level = logging.DEBUG if args.debug else logging.INFO
logging.basicConfig(filename=log_file, filemode="w", level=level, format='%(asctime)s:%(levelname)s:%(threadName)s: %(message)s') 

# Test and get AMQP configuration
if not args.loadpickles:
	for option in ["amqp_host", "amqp_port", "amqp_exchange", "amqp_queue", "amqp_user", "amqp_password", "amqp_routing_key"]:
	    if option not in scp.options("amqp"):
		print("Missing option %s in AMQP confguration." % option)
		config_read_fail = True
	amqp_host = scp.get("amqp", "amqp_host") if not scp.get("amqp", "amqp_host") == "" else "localhost"
	amqp_port = scp.getint("amqp", "amqp_port") if not scp.get("amqp", "amqp_port") == "" else 8336
	amqp_exchange = scp.get("amqp", "amqp_exchange") if not scp.get("amqp", "amqp_exchange") == "" else "namecoin"
	amqp_queue = scp.get("amqp", "amqp_queue") if not scp.get("amqp", "amqp_queue") == "" else "namecoin"
	amqp_user = scp.get("amqp", "amqp_user") if not scp.get("amqp", "amqp_user") == "" else "guest"
	amqp_password = scp.get("amqp", "amqp_password") if not scp.get("amqp", "amqp_password") == "" else "guest"
	credentials = pika.PlainCredentials(amqp_user, amqp_password)
	parameters = pika.ConnectionParameters(host=amqp_host, port=amqp_port, virtual_host="/", credentials=credentials)


# Test and get DB configuration
for db_option in ["db_host", "db_port", "db_user", "db_password", "db_name", "db_schema"]:
    if db_option not in scp.options("db"):
        print("Missing option %s in DB section in config file." % db_option)
        config_read_fail = True
    else:
        db_host = scp.get("db", "db_host") if not scp.get("db", "db_host") == "" else "localhost"
        db_port = scp.getint("db", "db_port") if not scp.get("db", "db_port") == "" else 5432
        db_user = scp.get("db", "db_user") if not scp.get("db", "db_user") == "" else  "blockchain"
        db_password= scp.get("db", "db_password") if not scp.get("db", "db_password") == "" else ""
        db_name= scp.get("db", "db_name") if not scp.get("db", "db_name") == "" else "blockchain"
        db_schema= scp.get("db", "db_schema") if not scp.get("db", "db_schema") == "" else "namecoin"

if config_read_fail:
    sys.exit(-1)


# set up AMQP
if not args.loadpickles:
    connection = pika.BlockingConnection(parameters=parameters)
    channel = connection.channel()
    channel.exchange_declare(amqp_exchange, type="fanout")
    channel.queue_declare(queue=amqp_queue)
    channel.queue_bind(exchange=amqp_exchange, queue=amqp_queue)


# set up DB connection
if not dry_run:
    connect_string = "port='" + str(db_port) + "' dbname='" + db_name + "' user='" + db_user + "' host='" + db_host + "' password='" + db_password + "'"
    conn = psycopg2.connect(connect_string)
    cursor = conn.cursor()



query_counter = 0
def db_query_execute(query, parms):
    global query_counter
    if dry_run:
        if parms is not None:
            print(query % parms)
            logging.info(query % parms)
        else:
            print(query)
    else:
        try:
            if parms is not None:
                res = cursor.execute(query, parms)
                logging.debug(query % parms)
            else:
                res = cursor.execute(query)
                logging.debug(query)
            conn.commit()
            query_counter = query_counter + 1
            print("Number of queries so far: %s \r" % query_counter)
        except psycopg2.Error, e:
            # Test for violation of uniqueness constraint
            if e.pgcode == '23505':
                logging.error("Error code is %s. Query was:" % e.pgcode)
                logging.error(query % parms)
                conn.commit()
                raise e
            else:
                print("Exception when running query. See log for details.")
                logging.error(e)
                logging.error(query % parms)
                sys.exit(-1)
        return res






def data_insert(body):
    block = OrderedDict(body["block"])
    # retrieve the parsed TXs, sort them by index, and store as OrderedDict
    parsed_txs_tmp = body["parsed_txs"]
    parsed_txs = OrderedDict()
    for key in sorted(parsed_txs_tmp):
        parsed_txs[key] = parsed_txs_tmp[key]

    if "auxpow" in body["block"]:
        auxpow = body["auxpow"]
    else:
        auxpow = None
    tx_volume = do_compute_tx_volume(parsed_txs)
    tx_fees = do_compute_tx_fee_volume(parsed_txs)

    # We first insert the block
    do_insert_block(block, tx_volume, tx_fees)
    # we next insert the TX
    for tx_index in parsed_txs:
        do_insert_tx(parsed_txs[tx_index])
    # now the auxpow, which will in turn add one more TX (the coinbase TX of the merge-mined block)
    if auxpow is not None:
        do_insert_auxpow(auxpow)
    # finally, the vout
    do_insert_vouts(parsed_txs)
    # and the spk
    do_insert_spks(parsed_txs)
    # and the vins
    do_insert_vins(parsed_txs)




def amqp_callback(ch, method, properties, body):
    body_json = json.loads(body, object_pairs_hook=OrderedDict)
    data_insert(body_json)


def pickle_insert(path):
    # create list of all pickle files in path
    pickle_list = os.listdir(path)
    # files are named by block index, we test if they exist and import
    for i in range(len(pickle_list)):
        pickle_fn_no_path = str(i) + ".pickle"
        pickle_fn = path + "/" + pickle_fn_no_path
        if pickle_fn_no_path not in pickle_list:
            logging.error("Pickle %s not found." % pickle_fn)
            print("Pickle %s not found." % pickle_fn)
            sys.exit(-1)
        with open(pickle_fn, "rb") as pickle_fh:
            body_json = pickle.load(pickle_fh)
            data_insert(body_json)



def do_insert_vins(parsed_txs):
    for tx_index in parsed_txs:
        tx = parsed_txs[tx_index]
        tx_id = tx["txid"]
        for vin in tx["vin"]:
            coinbase = vin["coinbase"] if "coinbase" in vin else None
            script_sig_dec = vin["scriptSig"]["dec"] if "scriptSig" in vin else None
            ref_tx_id = vin["txid"] if "txid" in vin else None
            ref_vout_n = vin["vout"] if "vout" in vin else None
            sequence = vin["sequence"] if "sequence" in vin else None
            sql_insert_vin = "INSERT INTO " + db_schema + ".vins (coinbase, script_sig, ref_tx_id, ref_vout_n, sequence, tx_id) VALUES (%s, %s, %s, %s, %s, %s)"
            db_query_execute(sql_insert_vin, (coinbase, json.dumps(script_sig_dec), ref_tx_id, ref_vout_n, sequence, tx_id))



def do_insert_spks(parsed_txs):
    for tx_index in parsed_txs:
        tx = parsed_txs[tx_index]
        tx_id = tx["txid"]
        for vout in tx["vout"]:
            vout_n = vout["n"]
            spk = vout["scriptPubKey"]
            addresses = spk["addresses"] if "addresses" in spk else []
            asm = spk["asm"]
            hex = spk["hex"]
            req_sigs = spk["reqSigs"] if "reqSigs" in spk else None
            type = spk["type"]

            # First, let's insert addresses
            for address in addresses:
                is_valid = parsed_txs[tx_index]["addresses_valid"][address]
                sql_insert_address = "INSERT INTO " + db_schema + ".addresses (address, block_first_seen, is_valid) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING"
                db_query_execute(sql_insert_address, (address, tx["block_hash"], is_valid))

            # Now, let's go for the spk
            if addresses == []:
                addresses = None
            sql_insert_spk = "INSERT INTO " + db_schema + ".spks (addresses, asm, hex, req_sigs, tx_id, type, vout_n) VALUES (%s, %s, %s, %s, %s, %s, %s)"
            # Just as with TX, we need to check for duplicate TX ID.
            # See do_insert_tx() for details.
            try:
                db_query_execute(sql_insert_spk, (addresses, asm, hex, req_sigs, tx_id, type, vout_n))
            except psycopg2.Error, e:
                logging.error("While inserting an SPK, the uniqueness constraint for TX %s was violated" % tx_id)
                logging.error("Doing an UPDATE instead of an INSERT.")
                sql_update_spk = "UPDATE " + db_schema + ".spks SET addresses = %s, asm = %s, hex = %s, req_sigs = %s, type = %s WHERE tx_id = %s AND vout_n = %s"
                db_query_execute(sql_update_spk, (addresses, asm, hex, req_sigs, type, tx_id, vout_n))

            # now let's check for name_ops, which are part of the SPK
            if "nameOp" in spk:
                name_op = spk["nameOp"]
                op = name_op["op"]

                # name_new is the announcement of an upcoming registration 
                if op == "name_new":
                    hash = name_op["hash"]
                    sql_insert_name_op = "INSERT INTO " + db_schema + ".name_ops (block_hash, hash, op, tx_id, vout_n) VALUES (%s, %s, %s, %s, %s)"
                    # Just as with TX, we need to check for duplicate TX ID.
                    # See do_insert_tx() for details.
                    try:
                        db_query_execute(sql_insert_name_op, (tx["block_hash"], hash, op, tx_id, vout_n))
                    except psycopg2.Error, e:
                        logging.error("While inserting a name_new op, the uniqueness constraint for TX %s was violated" % tx_id)
                        logging.error("Doing an UPDATE instead of an INSERT.")
                        sql_update_name_op = "UPDATE " + db_schema + ".name_ops SET block_hash = %s, hash = %s, op = %s WHERE tx_id = %s AND vout_n = %s"
                        db_query_execute(sql_update_name_op, (tx["block_hash"], hash, op, tx_id, vout_n))

                # name_firstupdate is the actual registration, name_update is the renewal
                elif op == "name_firstupdate" or op == "name_update":
                    rand = name_op["rand"] if "rand" in name_op else None
                    if "/" in name_op["name"]:
                        parts = name_op["name"].split("/")
                        namespace = parts[0]
                        if len(parts) > 2:
                            name = "".join(parts[1:])
                        else:
                            name = parts[1]
                    else:
                        namespace = ""
                        name = name_op["name"]
                    # The value can be very complex - we dump to JSON
                    value = name_op["value"]
                    sql_insert_name_op = "INSERT INTO " + db_schema + ".name_ops (block_hash, name, namespace, op, rand, tx_id, value, vout_n) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                    # Just as with TX, we need to check for duplicate TX ID.
                    # See do_insert_tx() for details.
                    try:
                        db_query_execute(sql_insert_name_op, (tx["block_hash"], name, namespace, op, rand, tx_id, json.dumps(value), vout_n))
                    except psycopg2.Error, e:
                        logging.error("While inserting a name_[first_]update op, the uniqueness constraint for TX %s was violated" % tx_id)
                        logging.error("Doing an UPDATE instead of an INSERT.")
                        sql_update_name_op = "UPDATE " + db_schema + ".name_ops SET block_hash = %s, name = %s, namespace = %s, op = %s, rand = %s, value = %s WHERE tx_id = %s AND vout_n = %s"
                        db_query_execute(sql_update_name_op, (tx["block_hash"], name, namespace, op, rand, json.dumps(value), tx_id, vout_n))

                # This is an operation that is not commonly used - dump all to JSON
                else:
                    sql_insert_name_op = "INSERT INTO " + db_schema + ".rare_name_ops (block_hash, json_dump, tx_id, vout_n) VALUES (%s, %s, %s, %s)"
                    # Just as with TX, we need to check for duplicate TX ID.
                    # See do_insert_tx() for details.
                    try:
                        db_query_execute(sql_insert_name_op, (tx["block_hash"], json.dumps(name_op), tx_id, vout_n))
                    except psycopg2.Error, e:
                        logging.error("While inserting a name_op, the uniqueness constraint for TX %s was violated" % tx_id)
                        logging.error("Doing an UPDATE instead of an INSERT.")
                        sql_update_name_op = "UPDATE " + db_schema + ".rare_name_ops SET block_hash = %s, json_dump = %s WHERE tx_id = %s AND vout_n = %s"
                        db_query_execute(sql_update_name_op, (tx["block_hash"], json.dumps(name_op), tx_id, vout_n))




def do_insert_vouts(parsed_txs):
    for tx_index in parsed_txs:
        tx = parsed_txs[tx_index]
        tx_id = tx["txid"]
        for vout in tx["vout"]:
            value = btc_to_swartz(vout["value"])
            vout_n = vout["n"]
            sql_insert_vout = "INSERT INTO " + db_schema + ".vouts (tx_id, value, vout_n) VALUES (%s, %s, %s)"
            # Just as with TX, we need to check for duplicate TX ID.
            # See do_insert_tx() for details.
            try:
                db_query_execute(sql_insert_vout, (tx_id, value, vout_n))
            except psycopg2.Error, e:
                logging.error("While inserting a vout, the uniqueness constraint for TX %s was violated" % tx_id)
                logging.error("Doing an UPDATE instead of an INSERT.")
                sql_update_vout = "UPDATE " + db_schema + ".vouts SET value = %s, vout_n = %s WHERE tx_id = %s"
                db_query_execute(sql_update_vout, (value, vout_n, tx_id))


def do_insert_tx(tx, auxpow_tx = False):
    if auxpow_tx:
        # Careful - this is NOT the same as the block_hash we inserted.
        # Nor is it the hash of the Bitcoin block that was merge-mined.
        # Rather, it's the hash over the NMC transactions.
        aux_block_header_hash = tx["blockhash"]
        # An auxpow is always a coinbase TX, so we can grab the value from the vouts
        tx_fee = helper_compute_vout_sum(tx)
    else:
        aux_block_header_hash = None
        tx_fee = tx["tx_fee"]
    # We computed this in the do_compute_tx_fee_volume
    block_hash = tx["block_hash"]
    lock_time = tx["locktime"]
    size = tx["size"]
    tx_id = tx["txid"]
    tx_index = tx["tx_index"]
    version = tx["version"]
    sql_insert_tx = "INSERT INTO " + db_schema + ".transactions (aux_block_header_hash, block_hash, fee, lock_time, size, tx_id, tx_index, version) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
    logging.debug("INSERT TX %s " % tx_id)
    try:
        db_query_execute(sql_insert_tx, (aux_block_header_hash, block_hash, tx_fee, lock_time, size, tx_id, tx_index, version))
    except psycopg2.Error, e:
        # It is a known phenomenon (and bug) that TX with the same ID exist in more than one block.
        # The blockchain's index stores only the last one and the previous one must be completely spent
        # (which it generally is). For all other purposes, it is "overwritten" in the blockchain.
        # We deal with this in exactly the same way - since the TX is spent, we can simply store one copy.
        # There is no way for us to get the older one anyway: the blockchain's index does not retrieve it
        # for us.
        logging.error("Uniqueness constraint for TX %s violated" % tx_id)
        logging.error("Doing an UPDATE instead of an INSERT.")
        sql_update_tx = "UPDATE " + db_schema + ".transactions SET aux_block_header_hash = %s, block_hash = %s, fee = %s, lock_time =%s, size = %s, tx_index = %s, version = %s WHERE tx_id = %s"
        db_query_execute(sql_update_tx, (aux_block_header_hash, block_hash, tx_fee, lock_time, size, tx_index, version, tx_id))



def do_insert_auxpow(auxpow):
    block_hash = auxpow["block_hash"]
    chain_index = auxpow["chainindex"]
    chain_merkle_branch = auxpow["chainmerklebranch"]
    index = auxpow["index"]
    merkle_branch = auxpow["merklebranch"]
    parent_block = auxpow["parentblock"]
    # this is the coinbase TX
    tx = auxpow["tx"]
    tx_id = tx["txid"]
    # First, insert coinbase TX of merge-mined block
    do_insert_tx(tx, auxpow_tx = True)
    # then insert auxpow fields
    sql_insert_aux = "INSERT INTO " + db_schema + ".auxpow (block_hash, chain_index, chain_merkle_branch, index, merkle_branch, parent_block, tx_id) VALUES (%s, %s, %s, %s, %s, %s, %s)"
    db_query_execute(sql_insert_aux, (block_hash, chain_index, chain_merkle_branch, index, merkle_branch, parent_block, tx_id))



# TODO Meticulously check in DB if this works correctly
def do_compute_tx_fee(tx_index, parsed_txs):
    # Get the TX we are working on
    tx = parsed_txs[tx_index]
    # Get the output sum in swartz.
    sum_vout = helper_compute_vout_sum(tx)
    logging.debug("Sum of all outputs for TX %s: %s" % (tx["txid"], sum_vout))
    
    # Get the input sum:
    # Take all vin, get the vout and tx_id they are referring to.
    # Many tx_id will already be in the DB. Some, however, may be in the current
    # parsed_txs.
    vout_dict = {}
    vin_counter = 0
    for vin in tx["vin"]:
        if "vout" in vin and "txid" in vin:
            vout_dict[vin_counter] = { "ref_tx_id" : vin["txid"], "ref_vout_n" : vin["vout"] }
            vin_counter = vin_counter + 1

    # if no vouts are found at all: this is a pure coinbase TX, return sum_vout
    if len(vout_dict) == 0:
        tx["tx_fee"] = sum_vout
        return tx

    # Fetch the txs, look in the vouts, add up (step 1: search in DB)
    where_cond = "(tx_id = '%s' AND vout_n = %s)"
    where_conds = []
    for vin_counter in vout_dict:
        vout_data = vout_dict[vin_counter]
        ref_tx_id = vout_data["ref_tx_id"]
        ref_vout_n = vout_data["ref_vout_n"]
        where_conds.append(where_cond % (ref_tx_id, ref_vout_n))
    logging.debug("where_conds for TX %s are: %s" % (tx["txid"], where_conds))
    where_clause = "WHERE "
    for i in range(0, len(where_conds) - 1):
        where_clause = where_clause + where_conds[i] + " OR "
    where_clause = where_clause + where_conds[-1]
    sql_get_tx_vout = "SELECT SUM(value) FROM " + db_schema + ".vouts " + where_clause
    db_query_execute(sql_get_tx_vout, None)
    if not dry_run:
        db_res = cursor.fetchone()
    else:
        # We don't care about the correct value in a dry-run. It just shouldn't
        # be 0, as there is a final sanity check at the end of this function.
        db_res = -99999999

    # Step 2: it may absolutely be the case that we do not find a single
    # referenced TX in the DB. In such a case, all referenced TX are in the
    # same block.
    if not dry_run:
        if db_res[0] is None:
            db_res = 0
            logging.info("We found a SUM of vouts to be NULL - check TX in the following string: " + sql_get_tx_vout)
            logging.info("The referring TX is %s" % tx["txid"])
        else:
            db_res = db_res[0]
        logging.debug("Computed fee found in DB in TX %s: %s" % (tx["txid"], db_res))

    # The block is not stored to DB yet at this time. So look in parsed_txs
    # if there are some TX that our vins are referencing.
    same_block_res = 0
    for vin_counter in vout_dict:
        vout_data = vout_dict[vin_counter]
        ref_tx_id = vout_data["ref_tx_id"]
        ref_vout_n = vout_data["ref_vout_n"]
        for tx_index in parsed_txs:
            if parsed_txs[tx_index]["txid"] == ref_tx_id:
                # We found a referenced TX in the same block
                for vout in parsed_txs[tx_index]["vout"]:
                    if vout["n"] == ref_vout_n:
                        same_block_res = same_block_res + btc_to_swartz(vout["value"])
                        logging.debug("Computed fees found in current block for TX %s: %s" % (tx["txid"], same_block_res))
    
    sum_vins = db_res + same_block_res
    # Do a sanity check. TX with vin 0 can exist, but are rare. We log them.
    if sum_vins == 0.0:
        logging.info("Sum of all inputs, i.e. referenced vouts in TX %s is 0. That's unusual." % tx["txid"])
  
    logging.debug("Sum of all inputs for TX %s is %s" % (tx["txid"], sum_vins))

    # We add a new field to the TX
    if not dry_run:
        tx["tx_fee"] = sum_vins - sum_vout
    else:
        tx["tx_fee"] = sum_vins - sum_vout

    if not dry_run and tx["tx_fee"] < 0:
        logging.error("Whoa. Negative TX fee in TX %s. Double-check." % tx["txid"])
        logging.error("In: %s" % sum_vins)
        logging.error("Out: %s" % sum_vout)
        raise ValueError("Negative TX fee")

    logging.debug("Overall fee for TX %s: %s" % (tx["txid"], tx["tx_fee"]))
    return tx


def do_compute_tx_fee_volume(parsed_txs):
    # sum over all do_compute_tx_fee(tx) for tx in block
    fees_volume = 0
    for tx_index in parsed_txs:
        try:
            tx = do_compute_tx_fee(tx_index, parsed_txs)
        except ValueError, e:
            print(e)
            logging.error("Invalid fee was found in block " + tx["block_hash"])
            channel.close()
            sys.exit(-1)
        fees_volume = fees_volume + tx["tx_fee"]
    return fees_volume



def do_compute_tx_volume(parsed_txs):
    tx_volume = 0
    for tx_index in parsed_txs:
        tx = parsed_txs[tx_index]
        tx_volume = tx_volume + helper_compute_vout_sum(tx)
    return tx_volume



def do_insert_block(block, tx_volume, tx_fees):
    bits = block["bits"]
    block_hash = block["hash"]
    block_index= block["height"]
    difficulty = block["difficulty"]
    median_time = block["mediantime"]
    nonce = block["nonce"]
    if block_index == 0:
        prev_block_hash = None
    else:
        prev_block_hash = block["previousblockhash"]
    size = block["size"]
    timestamp = block["time"]
    version = block["version"]

    sql_string = "INSERT INTO " + db_schema + ".blocks (bits, block_hash, block_index, difficulty, median_time, nonce, prev_block_hash, size, timestamp, tx_fees, tx_volume, version) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, (to_timestamp(%s) AT TIME ZONE 'UTC'), %s, %s, %s)"

    db_query_execute(sql_string, (bits, block_hash, block_index, difficulty, median_time, nonce, prev_block_hash, size, timestamp, tx_fees, tx_volume, version))



def helper_compute_vout_sum(tx):
    vout_sum = 0
    for vout in tx["vout"]:
        vout_sum = vout_sum + btc_to_swartz(vout["value"])
    return vout_sum


# These are the offical BTC conversion rules
# to address FP issues.
def btc_to_swartz(btc_val):
    return long(round(btc_val * 1e8))

def swartz_to_btc(swartz_val):
    return float(swartz_val / 1e8)


print(' [*] Waiting for logs. To exit press CTRL+C')
if not args.loadpickles:
    channel.basic_consume(amqp_callback, queue=amqp_queue, no_ack=True)
    channel.start_consuming()
else:
    if os.path.exists(args.loadpickles) and os.path.isdir(args.loadpickles):
        pickle_insert(args.loadpickles)
    else:
        print("Directory %s does not exist." % args.loadpickles)
        sys.exit(-1)
