#!/usr/bin/env python

"""
This module provides a session ticket mechanism.

The implemented mechanism is a subset of session tickets as proposed for
TLS in RFC 5077.

The format of a 112-byte ticket is:
 +------------+------------------+--------------+
 | 16-byte IV | 64-byte E(state) | 32-byte HMAC |
 +------------+------------------+--------------+

The 64-byte encrypted state contains:
 +-------------------+--------------------+--------------------+-------------+
 | 4-byte issue date | 18-byte identifier | 32-byte master key | 10-byte pad |
 +-------------------+--------------------+--------------------+-------------+
"""

import time
import const
import struct
import random
import datetime

from ..cryptoutils import (
    NewAESCipher, hmac_sha256_digest, AES_BLOCK_SIZE,
    get_random
)

from mycrypto import HMAC_SHA256_128

import logging

import util


def createTicketMessage(rawTicket, HMACKey):
    """
    Create and return a ready-to-be-sent ticket authentication message.

    Pseudo-random padding and a mark are added to `rawTicket' and the result is
    then authenticated using `HMACKey' as key for a HMAC.  The resulting
    authentication message is then returned.
    """

    assert len(rawTicket) == const.TICKET_LENGTH
    assert len(HMACKey) == const.TICKET_HMAC_KEY_LENGTH

    # Subtract the length of the ticket to make the handshake on
    # average as long as a UniformDH handshake message.
    padding = get_random(
        random.randint(
            0, const.MAX_PADDING_LENGTH - const.TICKET_LENGTH))

    mark = HMAC_SHA256_128(HMACKey, rawTicket)
    hmac = HMAC_SHA256_128(
        HMACKey, rawTicket + padding + mark + util.getEpoch())

    return rawTicket + padding + mark + hmac


def issueTicketAndKey(srvState):
    """
    Issue a new session ticket and append it to the according master key.

    The parameter `srvState' contains the key material and is passed on to
    `SessionTicket'.  The returned ticket and key are ready to be wrapped into
    a protocol message with the flag FLAG_NEW_TICKET set.
    """

    logging.info("Issuing new session ticket and master key.")
    masterKey = get_random(const.MASTER_KEY_LENGTH)
    newTicket = (SessionTicket(masterKey, srvState)).issue()

    return masterKey + newTicket


def checkKeys(srvState):
    """
    Check whether the key material for session tickets must be rotated.

    The key material (i.e., AES and HMAC keys for session tickets) contained in
    `srvState' is checked if it needs to be rotated.  If so, the old keys are
    stored and new ones are created.
    """

    assert (srvState.hmacKey is not None) and \
           (srvState.aesKey is not None) and \
           (srvState.keyCreation is not None)

    if (int(time.time()) - srvState.keyCreation) > const.KEY_ROTATION_TIME:
        logging.info("Rotating server key material for session tickets.")

        # Save expired keys to be able to validate old tickets.
        srvState.oldAesKey = srvState.aesKey
        srvState.oldHmacKey = srvState.hmacKey

        # Create new key material...
        srvState.aesKey = get_random(const.TICKET_AES_KEY_LENGTH)
        srvState.hmacKey = get_random(const.TICKET_HMAC_KEY_LENGTH)
        srvState.keyCreation = int(time.time())

        # ...and save it to disk.
        srvState.writeState()


def decrypt(ticket, srvState):
    """
    Decrypts, verifies and returns the given `ticket'.

    The key material used to verify the ticket is contained in `srvState'.
    First, the HMAC over the ticket is verified.  If it is valid, the ticket is
    decrypted.  Finally, a `ProtocolState()' object containing the master key
    and the ticket's issue date is returned.  If any of these steps fail,
    `None' is returned.
    """

    assert (ticket is not None) and (len(ticket) == const.TICKET_LENGTH)
    assert (srvState.hmacKey is not None) and (srvState.aesKey is not None)

    logging.debug("Attempting to decrypt and verify ticket.")

    checkKeys(srvState)

    # Verify the ticket's authenticity before decrypting.
    hmac = hmac_sha256_digest(srvState.hmacKey, ticket[0:80])
    if util.isValidHMAC(hmac, ticket[80:const.TICKET_LENGTH],
                        srvState.hmacKey):
        aesKey = srvState.aesKey
    else:
        if srvState.oldHmacKey is None:
            return None

        # Was the HMAC created using the rotated key material?
        oldHmac = hmac_sha256_digest(srvState.oldHmacKey, ticket[0:80])
        if util.isValidHMAC(oldHmac, ticket[80:const.TICKET_LENGTH],
                            srvState.oldHmacKey):
            aesKey = srvState.oldAesKey
        else:
            return None

    # Decrypt the ticket to extract the state information.
    aes = NewAESCipher(
        aesKey, ticket[0:const.TICKET_AES_CBC_IV_LENGTH]
    )
    plainTicket = aes.decrypt(ticket[const.TICKET_AES_CBC_IV_LENGTH:80])

    issueDate = struct.unpack('I', plainTicket[0:4])[0]
    identifier = plainTicket[4:22]
    masterKey = plainTicket[22:54]

    if not (identifier == const.TICKET_IDENTIFIER):
        logging.error("The ticket's HMAC is valid but the identifier is invalid.  "
                  "The ticket could be corrupt.")
        return None

    return ProtocolState(masterKey, issueDate=issueDate)


class ProtocolState(object):

    """
    Defines a ScrambleSuit protocol state contained in a session ticket.

    A protocol state is essentially a master key which can then be used by the
    server to derive session keys.  Besides, a state object contains an issue
    date which specifies the expiry date of a ticket.  This class contains
    methods to check the expiry status of a ticket and to dump it in its raw
    form.
    """

    def __init__(self, masterKey, issueDate=int(time.time())):
        """
        The constructor of the `ProtocolState' class.

        The four class variables are initialised.
        """

        self.identifier = const.TICKET_IDENTIFIER
        self.masterKey = masterKey
        self.issueDate = issueDate
        # Pad to multiple of 16 bytes to match AES' block size.
        self.pad = "\0\0\0\0\0\0\0\0\0\0"

    def isValid(self):
        """
        Verifies the expiry date of the object's issue date.

        If the expiry date is not yet reached and the protocol state is still
        valid, `True' is returned.  If the protocol state has expired, `False'
        is returned.
        """

        assert self.issueDate

        lifetime = int(time.time()) - self.issueDate
        if lifetime > const.SESSION_TICKET_LIFETIME:
            logging.debug("The ticket is invalid and expired %s ago." %
                      str(datetime.timedelta(seconds=
                      (lifetime - const.SESSION_TICKET_LIFETIME))))
            return False

        logging.debug("The ticket is still valid for %s." %
                  str(datetime.timedelta(seconds=
                  (const.SESSION_TICKET_LIFETIME - lifetime))))
        return True

    def __repr__(self):
        """
        Return a raw string representation of the object's protocol state.

        The length of the returned representation is exactly 64 bytes; a
        multiple of AES' 16-byte block size.  That makes it suitable to be
        encrypted using AES-CBC.
        """

        return struct.pack('I', self.issueDate) + self.identifier + \
                           self.masterKey + self.pad


class SessionTicket(object):

    """
    Encrypts and authenticates an encapsulated `ProtocolState()' object.

    This class implements a session ticket which can be redeemed by clients.
    The class contains methods to initialise and issue session tickets.
    """

    def __init__(self, masterKey, srvState):
        """
        The constructor of the `SessionTicket()' class.

        The class variables are initialised and the validity of the symmetric
        keys for the session tickets is checked.
        """

        assert (masterKey is not None) and \
               len(masterKey) == const.MASTER_KEY_LENGTH

        checkKeys(srvState)

        # Initialisation vector for AES-CBC.
        self.IV = get_random(const.TICKET_AES_CBC_IV_LENGTH)

        # The server's (encrypted) protocol state.
        self.state = ProtocolState(masterKey)

        # AES and HMAC keys to encrypt and authenticate the ticket.
        self.symmTicketKey = srvState.aesKey
        self.hmacTicketKey = srvState.hmacKey

    def issue(self):
        """
        Returns a ready-to-use session ticket after prior initialisation.

        After the `SessionTicket()' class was initialised with a master key,
        this method encrypts and authenticates the protocol state and returns
        the final result which is ready to be sent over the wire.
        """

        self.state.issueDate = int(time.time())

        # Encrypt the protocol state.
        aes = NewAESCipher(self.symmTicketKey, self.IV)
        state = repr(self.state)
        assert (len(state) % AES_BLOCK_SIZE) == 0
        cryptedState = aes.encrypt(state)

        # Authenticate the encrypted state and the IV.
        hmac = hmac_sha256_digest(self.hmacTicketKey, self.IV + cryptedState)

        finalTicket = self.IV + cryptedState + hmac
        logging.debug("Returning %d-byte ticket." % len(finalTicket))

        return finalTicket


# Alias class name in order to provide a more intuitive API.
new = SessionTicket
