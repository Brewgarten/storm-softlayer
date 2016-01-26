def test_vlan(softlayerDriver):

    # sanity check that we do not get back any vlans
    vlans = softlayerDriver.ex_get_vlans(includePrivate=False, includePublic=False)
    assert len(vlans) == 0

    privateVlans = softlayerDriver.ex_get_vlans(includePrivate=True, includePublic=False)
    for vlan in privateVlans:
        subnetIdentifiers = [subnet["networkIdentifier"]
                             for subnet in vlan.get("subnets", [])]
        assert all([identifier.startswith("10.") for identifier in subnetIdentifiers])

    publicVlans = softlayerDriver.ex_get_vlans(includePrivate=False, includePublic=True)
    for vlan in publicVlans:
        subnetIdentifiers = [subnet["networkIdentifier"]
                             for subnet in vlan.get("subnets", [])]
        assert all([not identifier.startswith("10.") for identifier in subnetIdentifiers])

    # make sure this includes all private and public vlans
    vlans = softlayerDriver.ex_get_vlans()
    assert set([vlan["id"] for vlan in vlans]) == set([vlan["id"] for vlan in privateVlans] + [vlan["id"] for vlan in publicVlans])
